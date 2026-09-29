import re
import shlex
import asyncio
import subprocess
import requests

from telegram import Update

from config import (
    TERRAGRUNT_DIR, ZEROTRUST_ACCOUNTS, GITLAB_URL, GITLAB_TOKEN,
    GITLAB_PROJECT_ID, GIT_BRANCH, ALLOWED_GROUP_ID,
)
from state import (
    executor, awaiting_whitelist_text, awaiting_whitelist_merchant_name,
    awaiting_whitelist_email, awaiting_vpn_text,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

def is_valid_ip(ip: str) -> bool:
    pattern = r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$|^[0-9a-fA-F:]+(/\d{1,3})?$"
    return bool(re.match(pattern, ip))

def is_valid_email(email: str) -> bool:
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    return bool(re.match(pattern, email))

EMAIL_DEFAULT_DOMAIN = "g-pay.vn"

def normalize_email(email: str) -> str:
    """Gõ tắt kiểu 'hieudd' (không có @) -> tự thêm domain mặc định 'hieudd@g-pay.vn'.
    Email đã có @ (kể cả domain khác g-pay.vn) thì giữ nguyên."""
    email = email.strip()
    return email if "@" in email else f"{email}@{EMAIL_DEFAULT_DOMAIN}"

def extract_domain(desc: str) -> str:
    """Lấy domain trong ngoặc từ desc, vd 'API cũ (mpa.g-pay.vn)' -> 'mpa.g-pay.vn'."""
    m = re.search(r'\(([^)]+)\)', desc)
    return m.group(1) if m else desc

def list_label(lst: dict) -> str:
    """Tên hiển thị cho người dùng của 1 list — ưu tiên domain lấy từ desc (dễ hiểu hơn
    tên kỹ thuật), fallback về name nếu desc không có domain trong ngoặc."""
    return extract_domain(lst.get("desc", "")) or lst.get("name", "")


def run_cmd(args: list, cwd: str = None) -> tuple[int, str]:
    # args là list argv, chạy KHÔNG qua shell → không có command injection
    printable = " ".join(shlex.quote(a) for a in args)
    try:
        result = subprocess.run(
            args,
            cwd=cwd or TERRAGRUNT_DIR,
            capture_output=True, text=True, timeout=900
        )
        output = (result.stdout or "") + (result.stderr or "")
        print(f"[CMD] {printable}\n{output}", flush=True)
        return result.returncode, output
    except subprocess.TimeoutExpired as e:
        def _dec(v):
            if not v:
                return ""
            return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
        output = _dec(e.stdout) + _dec(e.stderr)
        print(f"[TIMEOUT] {printable}\n{output}", flush=True)
        # Auto xóa state lock nếu bị timeout
        lock_path = _detect_lock_path(cwd or TERRAGRUNT_DIR)
        if lock_path:
            print(f"[AUTO-UNLOCK] Xóa lock: {lock_path}", flush=True)
            subprocess.run(["gsutil", "rm", lock_path], timeout=30)
        return -1, f"⏱ Timeout sau 900 giây.\n{output[-500:]}"


def _detect_lock_path(cwd: str) -> str:
    """Map thư mục terragrunt → GCS lock path tương ứng.
    Prefix state = 2 thành phần cuối của đường dẫn, vd .../g-pay-02/zero_trust → g-pay-02/zero_trust."""
    if not cwd:
        return ""
    known = [TERRAGRUNT_DIR] + [a.get("dir") for a in ZEROTRUST_ACCOUNTS]
    if cwd not in known:
        return ""
    prefix = "/".join(cwd.rstrip("/").split("/")[-2:])
    return f"gs://gpay-terraform-cloudflare-states/{prefix}/terraform.tfstate/default.tflock"

async def run_cmd_async(args: list, cwd: str = None) -> tuple[int, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: run_cmd(args, cwd))

def clean_output(output: str) -> str:
    clean = re.sub(r'\x1b\[[0-9;]*m', '', output)
    clean = re.sub(r'^\S+\s+STDOUT\s+terraform:\s+', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'^\S+\s+(INFO|ERROR|WARN)\s+.*\n?', '', clean, flags=re.MULTILINE)
    return clean.strip()

def compress_email_line(line: str) -> str:
    """
    Rút gọn dòng diff dạng:
      ~ identity = "identity.email in {\"a@x\" \"b@x\" ...}" -> "identity.email in {\"a@x\" \"b@x\" ... \"new@x\"}"
    Chỉ hiện 10 email đầu + số lượng còn lại + email được thêm/xoá.
    """
    m = re.match(
        r'(\s*~\s*identity\s*=\s*)"identity\.email in \{(.*?)\}"\s*->\s*"identity\.email in \{(.*?)\}"',
        line
    )
    if not m:
        return line

    prefix, old_part, new_part = m.groups()
    old_emails = re.findall(r'\\"([^\\"]+)\\"', old_part)
    new_emails = re.findall(r'\\"([^\\"]+)\\"', new_part)

    added   = [e for e in new_emails if e not in old_emails]
    removed = [e for e in old_emails if e not in new_emails]

    preview = old_emails[:10]
    extra = len(old_emails) - len(preview)
    preview_str = " ".join(f'"{e}"' for e in preview)
    if extra > 0:
        preview_str += f" ... (+{extra} email khác)"

    result = f'{prefix.strip()} "identity.email in {{ {preview_str} }}"'
    if added:
        result += "\n      ➕ Thêm: " + ", ".join(added)
    if removed:
        result += "\n      ➖ Xoá: " + ", ".join(removed)
    return result


def _filter_diff_lines(clean: str) -> str:
    """Chỉ giữ dòng diff THẬT SỰ thay đổi (bắt đầu bằng +/~/- , hoặc dòng
    ➕/➖ do compress_email_line sinh ra) + dòng header resource ("# ... will be
    ...") để biết đang sửa gì. Bỏ hết: context không đổi (id=..., name=...),
    "# (N ... hidden)", và cảnh báo -target/-out (không actionable với người dùng
    bot). Giống hệt terraform plan thật, chỉ lược phần không phải thay đổi thực
    sự — không tự bịa lại format riêng, nên áp dụng chung được cho mọi loại
    resource (whitelist lẫn VPN), không sợ "rớt" resource lạ không biết cấu trúc."""
    lines = []
    for raw_line in clean.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if "will be" in stripped:
                lines.append(line)
            continue
        # "(known after apply)" = Cloudflare/Terraform tự tính lại giá trị, không
        # phải thay đổi thật do bot gây ra (created_at, version, schedule...) — bỏ.
        if "(known after apply)" in stripped:
            continue
        if stripped[0] in "+~-" or stripped.startswith("➕") or stripped.startswith("➖"):
            lines.append(line)
    return "\n".join(lines)


def filter_plan_output(output: str, kind: str = "whitelist") -> tuple[str, bool]:
    # Provider v5: destroy IP riêng lẻ
    has_destroy = "is not in for_each map" in output

    # Provider v4: phân biệt xóa IP vĩnh viễn vs update comment
    # Update comment: có cả - item và + item với cùng IP → cho phép
    # Xóa thật: có - item nhưng KHÔNG có + item với cùng IP → block
    if not has_destroy:
        removed_ips = set(re.findall(r'-\s+ip\s*=\s*"([^"]+)"', output))
        added_ips   = set(re.findall(r'\+\s+ip\s*=\s*"([^"]+)"', output))
        truly_deleted = removed_ips - added_ips
        has_destroy = len(truly_deleted) > 0

    # Case 2: IP còn nhưng merchant bị xóa khỏi comment
    if not has_destroy:
        removed_blocks = re.findall(
            r'-\s+item\s*\{[^}]*comment\s*=\s*"([^"]*)"[^}]*ip\s*=\s*"([^"]+)"[^}]*\}',
            output, re.DOTALL
        )
        added_blocks = re.findall(
            r'\+\s+item\s*\{[^}]*comment\s*=\s*"([^"]*)"[^}]*ip\s*=\s*"([^"]+)"[^}]*\}',
            output, re.DOTALL
        )
        added_comment_by_ip = {ip: comment for comment, ip in added_blocks}
        for comment, ip in removed_blocks:
            if ip in added_comment_by_ip:
                old_merchants = set(m.strip() for m in comment.split(","))
                new_merchants = set(m.strip() for m in added_comment_by_ip[ip].split(","))
                lost = old_merchants - new_merchants
                if lost:
                    has_destroy = True
                    break

    clean = clean_output(output)

    no_change = "No changes. Your infrastructure matches the configuration."
    if no_change in clean:
        return "✅ No changes. Infrastructure matches the configuration.", False

    # Rút gọn dòng identity.email quá dài (nhiều email) trước khi lọc diff
    clean = "\n".join(compress_email_line(line) for line in clean.splitlines())

    m_plan = re.search(r'(Plan:\s*\d+ to add,\s*\d+ to change,\s*\d+ to destroy\.|Apply complete!.*)', clean)
    summary_line = m_plan.group(0) if m_plan else ""

    body = _filter_diff_lines(clean)
    if not body:
        # Không còn dòng diff nào sau khi lọc (bất thường) — fallback, không hiện trống.
        body = clean[-2000:] if len(clean) > 2000 else clean

    if len(body) > 3500:
        body = "... (đã rút gọn phần đầu)\n" + body[-3500:]

    result = f"{body}\n\n{summary_line}".strip() if summary_line else body
    return result, has_destroy

async def get_next_branch(identifier: str, today: str, repo_dir: str, prefix: str = "whitelist") -> str:
    """Tạo branch name với version tăng dần theo ngày."""
    _, remote_branches = await run_cmd_async(["git", "branch", "-r"], cwd=repo_dir)
    base = f"{prefix}/{identifier}-{today}-v"
    versions = []
    for line in remote_branches.splitlines():
        line = line.strip()
        if line.startswith(f"origin/{base}"):
            try:
                v = int(line.replace(f"origin/{base}", ""))
                versions.append(v)
            except:
                pass
    next_v = max(versions) + 1 if versions else 1
    return f"{prefix}/{identifier}-{today}-v{next_v}"


def create_gitlab_mr(branch: str, title: str) -> str:
    """Tạo MR trên GitLab, trả về URL hoặc thông báo lỗi."""
    print(f"[MR] Creating MR: branch={branch}, title={title}", flush=True)
    url = f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/merge_requests"
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    data = {
        "source_branch": branch,
        "target_branch": GIT_BRANCH,
        "title": title,
        "remove_source_branch": True,
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=30)
        print(f"[MR] Response: {resp.status_code} {resp.text[:300]}", flush=True)
        if resp.status_code in (200, 201):
            return resp.json().get("web_url", "MR created")
        return f"⚠️ GitLab API error {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        print(f"[MR] Exception: {e}", flush=True)
        return f"⚠️ Exception: {e}"


# ─── Guards ─────────────────────────────────────────────────────────────────

async def guard(update: Update) -> bool:
    chat_id = update.effective_chat.id
    awaiting_whitelist_text.discard(chat_id)
    awaiting_whitelist_merchant_name.discard(chat_id)
    awaiting_whitelist_email.discard(chat_id)
    awaiting_vpn_text.discard(chat_id)
    if update.effective_chat.id != ALLOWED_GROUP_ID:
        await update.message.reply_text("⛔ Bot chỉ hoạt động trong group GPay Whitelist.")
        return False
    return True
