import os
import re
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ZEROTRUST_ACCOUNTS, ZEROTRUST_EXCLUDED_TEAMS, GIT_BRANCH, ALLOWED_GROUP_ID
from state import pending, pending_vpn, awaiting_vpn_text
from common import is_valid_email, run_cmd_async, get_next_branch, filter_plan_output, guard


# ─── Zero Trust helpers ────────────────────────────────────────────────────

def extract_block(content: str, start_idx: int) -> tuple[int, int]:
    """
    Cho vị trí của dấu '{' tại start_idx, trả về (start_idx, end_idx)
    là vị trí dấu '}' tương ứng (đóng block, có brace-matching).
    """
    depth = 0
    i = start_idx
    while i < len(content):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                return start_idx, i
        i += 1
    return start_idx, len(content) - 1

def get_active_teams(content: str) -> list[dict]:
    """
    Lấy danh sách team (key) trong block network_gateway_policy
    mà KHÔNG bị comment (#) và không thuộc danh sách loại trừ.
    Trả về list dict {"name": ..., "domains": [...]} để hiện preview domain.
    """
    m = re.search(r'network_gateway_policy\s*=\s*\{', content)
    if not m:
        return []

    brace_start = content.index('{', m.end() - 1)
    _, brace_end = extract_block(content, brace_start)
    block = content[brace_start:brace_end]

    teams = []
    pos = 0
    pattern = re.compile(r'^[ \t]*"([^"]+)"[ \t]*=[ \t]*\{', re.MULTILINE)

    for match in pattern.finditer(block):
        name = match.group(1)
        if name in ZEROTRUST_EXCLUDED_TEAMS:
            continue

        # Tìm vị trí dấu '{' của block này để brace-match chính xác
        brace_pos = block.index('{', match.start())
        try:
            _, team_end = extract_block(block, brace_pos)
            team_text = block[brace_pos:team_end]
        except Exception:
            team_text = ""

        domains = []
        try:
            traffic_match = re.search(r'traffic\s*=\s*"((?:[^"\\]|\\.)*)"', team_text)
            if traffic_match:
                sni_match = re.search(r'sni\.host[^{]*\{([^}]*)\}', traffic_match.group(1))
                if sni_match:
                    domains = re.findall(r'"([^"]+)"', sni_match.group(1))
        except Exception:
            domains = []

        teams.append({"name": name, "domains": domains})

    return teams

def add_email_to_zerotrust(email: str, teams: list[str], zerotrust_file: str) -> dict:
    """
    Thêm email vào:
      1. access_group "Email list" -> include_email
      2. identity.email của từng team được chọn trong network_gateway_policy

    Trả về dict kết quả chi tiết.
    """
    with open(zerotrust_file, "r") as f:
        content = f.read()

    result = {"access_group": None, "teams": {}}

    # 1. access_group "Email list" -> include_email = [...]
    pattern_group = r'(include_email\s*=\s*\[)(.*?)(\])'
    match_group = re.search(pattern_group, content, re.DOTALL)
    if not match_group:
        result["access_group"] = "pattern_not_found"
    elif f'"{email}"' in match_group.group(2):
        result["access_group"] = "already_exists"
    else:
        inner = match_group.group(2).rstrip()
        if inner.endswith(","):
            inner = inner[:-1]
        inner = f'{inner}, "{email}"' if inner else f'"{email}"'
        content = content[:match_group.start(2)] + inner + content[match_group.end(2):]
        result["access_group"] = "added"

    # 2. Từng team trong network_gateway_policy
    escaped_email = f'\\"{email}\\"'
    for team in teams:
        team_pattern = r'"' + re.escape(team) + r'"\s*=\s*\{.*?identity\s*=\s*"identity\.email in \{(.*?)\}"'
        m = re.search(team_pattern, content, re.DOTALL)
        if not m:
            result["teams"][team] = "not_found"
            continue
        if escaped_email in m.group(1):
            result["teams"][team] = "already_exists"
            continue
        existing_inner = m.group(1).rstrip()
        new_inner = f'{existing_inner} {escaped_email}' if existing_inner else escaped_email
        start, end = m.span(1)
        content = content[:start] + new_inner + content[end:]
        result["teams"][team] = "added"

    with open(zerotrust_file, "w") as f:
        f.write(content)

    return result


# ─── /vpn ───────────────────────────────────────────────────────────────────

def build_team_checklist_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Mỗi team 1 nút riêng, hiện tên đầy đủ + trạng thái tick (☐/✅) ngay trên nút —
    không cần dò số ngược lại khối text phía trên."""
    data = pending_vpn[chat_id]
    selected = data["selected"]
    buttons = []
    for i, team in enumerate(data["teams"]):
        mark = "✅" if i in selected else "☐"
        label = f"{mark} {team['name']}"
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"vpn_toggle:{i}")])
    return InlineKeyboardMarkup(buttons)


def build_team_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Hàng Xác nhận/Huỷ — nút xác nhận hiện tên email TIẾP THEO trong queue (nếu còn),
    giống hệt cách /whitelist hỏi lần lượt từng merchant."""
    data = pending_vpn[chat_id]
    current_idx = data["current_idx"]
    total = len(data["queue"])

    if current_idx + 1 >= total:
        confirm_label = "✅ Xác nhận"
    else:
        confirm_label = f"➡️ Tiếp: {data['queue'][current_idx + 1]}"
        if len(confirm_label) > 60:
            confirm_label = confirm_label[:57] + "..."

    return InlineKeyboardMarkup([[
        InlineKeyboardButton(confirm_label, callback_data="vpn_confirm"),
        InlineKeyboardButton("❌ Huỷ", callback_data="vpn_cancel"),
    ]])


def build_account_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(a["name"], callback_data=f"vpn_acct:{i}")]
        for i, a in enumerate(ZEROTRUST_ACCOUNTS)
    ]
    buttons.append([InlineKeyboardButton("❌ Huỷ", callback_data="vpn_cancel")])
    return InlineKeyboardMarkup(buttons)


async def cmd_vpn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    if not ZEROTRUST_ACCOUNTS:
        await update.message.reply_text("❌ Chưa cấu hình `ZEROTRUST_ACCOUNTS`.", parse_mode="Markdown")
        return

    if not ctx.args:
        awaiting_vpn_text.add(update.effective_chat.id)
        await update.message.reply_text(
            "📌 Gõ (các) email ở tin nhắn tiếp theo (không cần gõ lại `/vpn`):\n"
            "`<email1> [email2] ...`\n\n"
            "VD: `user@gmail.com user2@gmail.com`\n\n"
            "Bot sẽ hỏi các email này cần cấp quyền cho team nào.",
            parse_mode="Markdown"
        )
        return

    await process_vpn_args(update, ctx, ctx.args)


def _emails_allowed_in(content: str) -> set:
    """Trả về tập email hiện có trong include_email của access_group "Email list"."""
    m = re.search(r'include_email\s*=\s*\[(.*?)\]', content, re.DOTALL)
    if not m:
        return set()
    return set(re.findall(r'"([^"]+)"', m.group(1)))


async def _check_existing_allow(emails: list[str]) -> dict:
    """Với mỗi account đã cấu hình, pull mới nhất rồi kiểm tra những email nào đã có
    sẵn trong include_email — trả về {email: [account_name, ...]} để báo lại user biết
    email đã được allow ở đâu trước khi chọn account cấp thêm, tránh cấp trùng/nhầm."""
    result = {e: [] for e in emails}
    for account in ZEROTRUST_ACCOUNTS:
        repo_dir = os.path.dirname(account["file"])
        await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
        await run_cmd_async(["git", "pull", "origin", GIT_BRANCH], cwd=repo_dir)
        try:
            with open(account["file"], "r") as f:
                content = f.read()
        except Exception as e:
            print(f"[VPN] Không đọc được {account['file']}: {e}", flush=True)
            continue
        existing_emails = _emails_allowed_in(content)
        for email in emails:
            if email in existing_emails:
                result[email].append(account["name"])
    return result


async def process_vpn_args(update: Update, ctx: ContextTypes.DEFAULT_TYPE, raw_emails: list[str]):
    # Chuẩn hoá về chữ thường — Cloudflare Access coi email case-insensitive, gõ hoa/thường
    # lẫn lộn dễ gây nhầm khi so khớp "đã tồn tại chưa" hoặc tạo diff rác trong file.
    emails = [e.strip().lower() for e in raw_emails if e.strip()]
    if not emails:
        await update.message.reply_text(
            "📌 *Usage:*\n`<email1> [email2] ...`\n\n"
            "VD: `user@gmail.com user2@gmail.com`",
            parse_mode="Markdown"
        )
        return

    invalid = [e for e in emails if not is_valid_email(e)]
    if invalid:
        await update.message.reply_text(
            f"❌ Email không hợp lệ: `{'`, `'.join(invalid)}`",
            parse_mode="Markdown"
        )
        return

    chat_id = update.effective_chat.id

    # Chỉ 1 account → vào thẳng bước chọn team; nhiều account → hỏi chọn account trước
    if len(ZEROTRUST_ACCOUNTS) == 1:
        pending_vpn[chat_id] = {"emails": emails, "account": ZEROTRUST_ACCOUNTS[0]}
        await vpn_load_teams(update.message, chat_id, ZEROTRUST_ACCOUNTS[0], emails)
        return

    pending_vpn[chat_id] = {"emails": emails, "account": None}

    await update.message.reply_text(
        f"🔄 Đang kiểm tra {len(emails)} email đã được allow ở account nào chưa..."
    )
    existing = await _check_existing_allow(emails)
    status_lines = []
    for email in emails:
        accounts_str = ', '.join(existing.get(email) or [])
        status_lines.append(
            f"📋 {email} đã được allow ở: {accounts_str}" if accounts_str
            else f"📋 {email} chưa được allow ở account nào"
        )

    await update.message.reply_text(
        "```\n" + "\n".join(status_lines) + "\n```\n\n"
        f"🌍 Chọn account để cấp VPN cho {len(emails)} email:",
        parse_mode="Markdown",
        reply_markup=build_account_keyboard(),
    )


async def send_team_selection_prompt(message, chat_id: int):
    """Hỏi team cho email HIỆN TẠI trong queue (current_idx) — hỏi lần lượt từng email
    (không gộp chung 1 bộ team cho cả batch) vì dù chung account, mỗi email vẫn có thể
    cần vào team khác nhau."""
    data = pending_vpn[chat_id]
    current_email = data["queue"][data["current_idx"]]
    data["selected"] = set()

    await message.reply_text(
        f"📋 Chọn team cho email `{current_email}` "
        f"({data['current_idx'] + 1}/{len(data['queue'])}, account `{data['account']['name']}`):\n"
        f"👆 Bấm để tick, bấm lại để bỏ:",
        parse_mode="Markdown",
        reply_markup=build_team_checklist_keyboard(chat_id),
    )
    await message.reply_text(
        "👉 Chọn xong thì bấm:",
        reply_markup=build_team_confirm_keyboard(chat_id),
    )


async def vpn_load_teams(message, chat_id: int, account: dict, emails: list[str]):
    """Pull repo của account, đọc file zero_trust, lấy team active, hỏi team lần lượt
    cho từng email trong danh sách."""
    try:
        await message.reply_text(
            f"🔄 Đang chuẩn bị cấp VPN (`{account['name']}`) cho *{len(emails)} email*:\n" +
            "\n".join(f"• `{e}`" for e in emails),
            parse_mode="Markdown"
        )

        repo_dir = os.path.dirname(account["file"])

        await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
        _, pull_out = await run_cmd_async(["git", "pull", "origin", GIT_BRANCH], cwd=repo_dir)

        _, log_out = await run_cmd_async(
            ["git", "log", "--oneline", "-5", "--pretty=format:%h %s (%an)"],
            cwd=repo_dir
        )
        await message.reply_text(
            f"📥 *Pulled latest `{GIT_BRANCH}`:*\n```\n{pull_out[:300]}\n```\n\n"
            f"📋 *5 commits mới nhất:*\n```\n{log_out}\n```",
            parse_mode="Markdown"
        )

        with open(account["file"], "r") as f:
            content = f.read()
        teams = get_active_teams(content)

        if not teams:
            pending_vpn.pop(chat_id, None)
            await message.reply_text("❌ Không tìm thấy team nào (active) trong `network_gateway_policy`.")
            return

        pending_vpn[chat_id] = {
            "emails": emails,
            "account": account,
            "teams": teams,
            "queue": emails,
            "current_idx": 0,
            "selected": set(),
            "selected_per_email": {},
        }

        await send_team_selection_prompt(message, chat_id)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()[-1500:]
        await message.reply_text(
            f"🚨 Lỗi khi chuẩn bị /vpn:\n```\n{e}\n```\n```\n{tb}\n```",
            parse_mode="Markdown"
        )


async def vpn_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id

    if chat_id != ALLOWED_GROUP_ID:
        await query.answer("⛔ Không có quyền.", show_alert=True)
        return

    data = pending_vpn.get(chat_id)
    if not data:
        await query.answer("⚠️ Yêu cầu đã hết hạn, gõ /vpn lại.", show_alert=True)
        return

    action = query.data

    try:
        if action == "vpn_cancel":
            pending_vpn.pop(chat_id, None)
            await query.answer()
            await query.edit_message_text("🔄 Đã huỷ.")
            return

        if action.startswith("vpn_acct:"):
            idx = int(action.split(":")[1])
            if idx < 0 or idx >= len(ZEROTRUST_ACCOUNTS):
                await query.answer("⚠️ Account không hợp lệ.", show_alert=True)
                return
            account = ZEROTRUST_ACCOUNTS[idx]
            await query.answer()
            await query.edit_message_text(f"🌍 Account: *{account['name']}*", parse_mode="Markdown")
            await vpn_load_teams(query.message, chat_id, account, data["emails"])
            return

        if action.startswith("vpn_toggle:"):
            idx = int(action.split(":")[1])
            selected = data["selected"]
            if idx in selected:
                selected.remove(idx)
                toast = f"☐ Bỏ tick #{idx + 1} {data['teams'][idx]['name']}"
            else:
                selected.add(idx)
                toast = f"✅ Đã tick #{idx + 1} {data['teams'][idx]['name']}"
            await query.answer(toast)
            await query.edit_message_reply_markup(reply_markup=build_team_checklist_keyboard(chat_id))
            return

        if action == "vpn_confirm":
            if not data["selected"]:
                await query.answer("⚠️ Chưa chọn team nào!", show_alert=True)
                return

            await query.answer()
            current_email = data["queue"][data["current_idx"]]
            selected_teams = [data["teams"][i]["name"] for i in sorted(data["selected"])]
            data["selected_per_email"][current_email] = selected_teams

            await query.edit_message_text(
                "```\n"
                f"✅ {current_email} → {', '.join(selected_teams)}"
                "\n```",
                parse_mode="Markdown"
            )

            data["current_idx"] += 1
            if data["current_idx"] < len(data["queue"]):
                # Còn email khác trong queue -> hỏi tiếp team cho email tiếp theo,
                # KHÔNG gộp chung team của email này cho email kia.
                await send_team_selection_prompt(query.message, chat_id)
            else:
                emails = data["emails"]
                account = data["account"]
                selected_per_email = data["selected_per_email"]
                pending_vpn.pop(chat_id, None)
                await vpn_finalize(query.message, emails, selected_per_email, account)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()[-1500:]
        await query.message.reply_text(
            f"🚨 Lỗi khi xử lý lựa chọn team:\n```\n{e}\n```\n```\n{tb}\n```",
            parse_mode="Markdown"
        )


async def vpn_finalize(message, emails: list[str], selected_per_email: dict, account: dict):
    chat_id = message.chat_id
    zt_dir = account["dir"]
    zt_file = account["file"]
    repo_dir = os.path.dirname(zt_file)

    # Dùng account + email đầu tiên làm identifier cho branch name
    identifier = re.sub(r'[^a-zA-Z0-9]', '-', f"{account['name']}-{emails[0]}")
    if len(emails) > 1:
        identifier += f"-and-{len(emails)-1}more"
    today = datetime.now().strftime("%Y%m%d")
    branch = await get_next_branch(identifier, today, repo_dir, prefix="vpn")
    await run_cmd_async(["git", "checkout", "-b", branch], cwd=repo_dir)

    # Add từng email vào ĐÚNG team đã chọn riêng cho email đó (có thể khác nhau
    # giữa các email dù chung 1 account).
    all_summary = []
    any_added = False
    for email in emails:
        teams = selected_per_email.get(email, [])
        result = add_email_to_zerotrust(email, teams, zt_file)
        ag = result["access_group"]
        teams_str = ", ".join(teams)
        if ag == "added":
            all_summary.append(f"✅ {email} → Email list + {teams_str}")
            any_added = True
        elif ag == "already_exists":
            # Kiểm tra xem có team nào được add không
            team_added = any(v == "added" for v in result["teams"].values())
            if team_added:
                all_summary.append(f"📝 {email} → đã có Email list, thêm vào team: {teams_str}")
                any_added = True
            else:
                all_summary.append(f"⚠️ {email} đã có đủ quyền, bỏ qua")
        elif ag == "pattern_not_found":
            all_summary.append(f"❌ {email} → không tìm thấy block Email list")

    if not any_added:
        await message.reply_text(
            "```\n" + "\n".join(all_summary) + "\n\nKhông có thay đổi nào, bỏ qua plan.\n```",
            parse_mode="Markdown"
        )
        await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
        await run_cmd_async(["git", "branch", "-D", branch], cwd=repo_dir)
        return

    await message.reply_text(
        "```\n" + "\n".join(all_summary) + f"\n\n🌿 Branch: {branch}\n⏳ Đang chạy terragrunt plan...\n```",
        parse_mode="Markdown"
    )

    plan_code, plan_output = await run_cmd_async(
        ["terragrunt", "plan", "-no-color", "--terragrunt-non-interactive"], cwd=zt_dir
    )
    log, has_destroy = filter_plan_output(plan_output, kind="vpn")

    if has_destroy:
        await run_cmd_async(["git", "checkout", "--", zt_file], cwd=repo_dir)
        await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
        await run_cmd_async(["git", "branch", "-D", branch], cwd=repo_dir)
        await message.reply_text(
            f"🚨 *Plan bị huỷ tự động!*\n\n"
            f"Phát hiện thay đổi bất thường (destroy).\n\n"
            f"```\n{log}\n```",
            parse_mode="Markdown"
        )
        return

    emails_str = ", ".join(emails)
    all_teams = sorted({t for ts in selected_per_email.values() for t in ts})
    pending[chat_id] = {
        "type": "vpn",
        "identifier": emails[0],
        "branch": branch,
        "file": zt_file,
        "terragrunt_dir": zt_dir,
        "commit_msg": f"[{account['name']}] open VPN access for {emails_str} ({', '.join(all_teams)})",
        "mr_title": f"[{account['name']}] open VPN access for {emails_str} ({', '.join(all_teams)})",
    }

    status = "✅ Plan thành công" if plan_code == 0 else "❌ Plan failed"
    await message.reply_text(
        f"{status}\n\n```\n{log}\n```\n\nGõ /apply để apply hoặc /cancel để huỷ.",
        parse_mode="Markdown"
    )
