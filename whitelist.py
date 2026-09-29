import os
import re
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import IP_FILE, GIT_BRANCH, TERRAGRUNT_DIR, MERCHANT_LISTS, ALLOWED_GROUP_ID
from state import (
    pending, pending_whitelist, awaiting_whitelist_text,
    awaiting_whitelist_merchant_name, awaiting_whitelist_email,
)
from common import (
    is_valid_ip, extract_domain, list_label, run_cmd_async, get_next_branch,
    filter_plan_output, guard,
)


def add_ip_to_file(ip: str, merchant: str, list_name: str = "merchant_ips") -> str:
    with open(IP_FILE, "r") as f:
        content = f.read()

    # Tìm IP trong list_name cụ thể
    # Pattern: tìm block list_name trước, rồi tìm IP trong đó
    list_pattern = rf'"{re.escape(list_name)}"\s*=\s*\{{.*?items\s*=\s*\[(.*?)\]'
    list_match = re.search(list_pattern, content, re.DOTALL)
    if not list_match:
        return "pattern_not_found"

    list_start = list_match.start(1)
    list_end = list_match.end(1)
    list_content = list_match.group(1)

    existing = re.search(rf'\{{ ip = "{re.escape(ip)}", comment = "([^"]*)" \}}', list_content)
    if existing:
        current_comment = existing.group(1)
        if merchant in current_comment:
            return "already_exists"
        new_comment = f"{current_comment}, {merchant}"
        new_list_content = list_content.replace(
            f'{{ ip = "{ip}", comment = "{current_comment}" }}',
            f'{{ ip = "{ip}", comment = "{new_comment}" }}'
        )
        new_content = content[:list_start] + new_list_content + content[list_end:]
        with open(IP_FILE, "w") as f:
            f.write(new_content)
        return "appended"

    new_entry = f'\n        {{ ip = "{ip}", comment = "{merchant}" }},'
    # Insert sau dấu [ của items
    items_match = re.search(
        rf'"{re.escape(list_name)}"\s*=\s*\{{.*?items\s*=\s*\[',
        content, re.DOTALL
    )
    if not items_match:
        return "pattern_not_found"

    insert_pos = items_match.end()
    new_content = content[:insert_pos] + new_entry + content[insert_pos:]
    with open(IP_FILE, "w") as f:
        f.write(new_content)
    return "added"


# ─── /whitelist ─────────────────────────────────────────────────────────────

async def cmd_whitelist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    raw_text = update.message.text or ""
    raw_args = re.sub(r'^/whitelist(@\w+)?\s*', '', raw_text, flags=re.IGNORECASE).strip()

    if "=" not in raw_args:
        awaiting_whitelist_text.add(update.effective_chat.id)
        await update.message.reply_text(
            "📌 Gõ danh sách merchant/IP ở tin nhắn tiếp theo, hoặc gửi ảnh chụp "
            "form đề nghị whitelist:\n"
            "```\n<merchant1> = <ip1> [ip2]\n<merchant2> = <ip3> [ip4]\n```\n\n"
            "VD:\n```\nMERCHANT_NAME01 = 1.2.3.4 5.6.7.8\nMERCHANT_NAME02 = 9.10.11.12\n```",
            parse_mode="Markdown"
        )
        return

    await process_whitelist_args(update, ctx, raw_args)


async def process_whitelist_args(update: Update, ctx: ContextTypes.DEFAULT_TYPE, raw_args: str):
    # Parse nhiều merchant: mỗi dòng có dạng "merchant = ip1 ip2 ..."
    # Hỗ trợ cả dạng 1 dòng và nhiều dòng
    merchant_ips = {}  # {merchant: [ip, ...]}
    errors = []

    lines = raw_args.splitlines()

    # Ghép lại các dòng không có dấu = vào dòng trước
    merged = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            merged.append(line)
        elif merged:
            merged[-1] += " " + line

    for line in merged:
        if "=" not in line:
            continue
        merchant_part, ip_part = line.split("=", 1)
        merchant = merchant_part.strip()
        ips = [ip.strip() for ip in ip_part.strip().split() if ip.strip()]
        if not merchant or not ips:
            continue
        invalid = [ip for ip in ips if not is_valid_ip(ip)]
        if invalid:
            errors.append(f"❌ `{merchant}`: IP không hợp lệ: `{'`, `'.join(invalid)}`")
            continue
        merchant_ips[merchant] = ips

    if errors:
        await update.message.reply_text("\n".join(errors), parse_mode="Markdown")
        if not merchant_ips:
            return

    if not merchant_ips:
        await update.message.reply_text(
            "📌 *Usage:*\n"
            "```\n/whitelist <merchant1> = <ip1> [ip2]\n<merchant2> = <ip3> [ip4]\n```\n\n"
            "VD:\n```\n/whitelist MERCHANT_NAME01 = 1.2.3.4 5.6.7.8\nMERCHANT_NAME02 = 9.10.11.12\n```",
            parse_mode="Markdown"
        )
        return

    total_ips = sum(len(v) for v in merchant_ips.values())
    await update.message.reply_text(
        f"🔄 Đang xử lý *{total_ips} IP* cho *{len(merchant_ips)} merchant*:\n" +
        "\n".join(f"• `{m}`: {len(ips)} IP" for m, ips in merchant_ips.items()),
        parse_mode="Markdown"
    )

    repo_dir = os.path.dirname(IP_FILE)

    await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
    _, pull_out = await run_cmd_async(["git", "pull", "origin", GIT_BRANCH], cwd=repo_dir)

    _, log_out = await run_cmd_async(
        ["git", "log", "--oneline", "-5", "--pretty=format:%h %s (%an)"],
        cwd=repo_dir
    )
    await update.message.reply_text(
        f"📥 *Pulled latest `{GIT_BRANCH}`:*\n```\n{pull_out[:300]}\n```\n\n"
        f"📋 *5 commits mới nhất:*\n```\n{log_out}\n```",
        parse_mode="Markdown"
    )

    # Lưu tạm merchant_ips, queue từng merchant để hỏi list + email người yêu cầu
    merchant_list = list(merchant_ips.items())
    pending_whitelist[update.effective_chat.id] = {
        "merchant_ips": merchant_ips,
        "requester_per_merchant": {},
        "merchant_name_per_merchant": {},
        "queue": merchant_list,
        "current_idx": 0,
        "selected_per_merchant": {},
    }

    first_merchant = merchant_list[0][0]
    chat_id = update.effective_chat.id
    await ask_merchant_name(update.message, chat_id, first_merchant)


async def whitelist_finalize(message, merchant_ips: dict, selected_per_merchant: dict,
                              requester_per_merchant: dict = None, merchant_name_per_merchant: dict = None):
    requester_per_merchant = requester_per_merchant or {}
    merchant_name_per_merchant = merchant_name_per_merchant or {}
    chat_id = message.chat_id
    repo_dir = os.path.dirname(IP_FILE)

    today = datetime.now().strftime("%Y%m%d")
    first_merchant = list(merchant_ips.keys())[0]
    branch_identifier = re.sub(r'\s+', '-', first_merchant.strip())
    branch_identifier = re.sub(r'[^a-zA-Z0-9\-_]', '', branch_identifier)
    if len(merchant_ips) > 1:
        branch_identifier += f"-and-{len(merchant_ips)-1}more"
    branch = await get_next_branch(branch_identifier, today, repo_dir, prefix="whitelist")
    await run_cmd_async(["git", "checkout", "-b", branch], cwd=repo_dir)

    # Add IP cho từng merchant vào list tương ứng
    all_summary = []
    any_new = False
    all_target_lists = set()

    for merchant, ips in merchant_ips.items():
        selected_lists = selected_per_merchant.get(merchant, [])
        all_target_lists.update(lst["name"] for lst in selected_lists)
        requester_str = ", ".join(requester_per_merchant.get(merchant, []))
        merchant_summary = [f"{merchant} (yêu cầu bởi {requester_str}):"]
        for lst in selected_lists:
            results = {"added": [], "appended": [], "already_exists": [], "error": []}
            for ip in ips:
                result = add_ip_to_file(ip, merchant, lst["name"])
                if result in results:
                    results[result].append(ip)
                else:
                    results["error"].append(ip)
            label = list_label(lst)
            if results["added"]:
                merchant_summary.append(f"  ✅ {label} thêm mới: {', '.join(results['added'])}")
                any_new = True
            if results["appended"]:
                merchant_summary.append(f"  📝 {label} append: {', '.join(results['appended'])}")
                any_new = True
            if results["already_exists"]:
                merchant_summary.append(f"  ⚠️ {label} đã tồn tại: {', '.join(results['already_exists'])}")
            if results["error"]:
                merchant_summary.append(f"  ❌ {label} lỗi: {', '.join(results['error'])}")
        all_summary.extend(merchant_summary)

    if not any_new:
        await message.reply_text(
            "```\n" + "\n".join(all_summary) + "\n\nKhông có IP mới, bỏ qua plan.\n```",
            parse_mode="Markdown"
        )
        await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
        await run_cmd_async(["git", "branch", "-D", branch], cwd=repo_dir)
        return

    # Build target flags từ tất cả list được chọn (union) — list argv, không qua shell
    target_flags = [
        f'-target=cloudflare_list.this["{lst_name}"]'
        for lst_name in all_target_lists
    ]

    merchants_str = ", ".join(merchant_ips.keys())

    # Build domain string từ các list được chọn
    all_domains = []
    for lst_name in sorted(all_target_lists):
        lst_info = next((l for l in MERCHANT_LISTS if l["name"] == lst_name), None)
        if lst_info:
            desc = lst_info.get("desc", "")
            domain = extract_domain(desc)
            if domain:
                all_domains.append(domain)
    domains_str = ", ".join(sorted(set(all_domains))) if all_domains else ", ".join(sorted(all_target_lists))

    # Nếu tất cả merchant cùng chung 1 (nhóm) người yêu cầu thì rút gọn,
    # ngược lại liệt kê rõ merchant nào do ai yêu cầu
    requester_sets = {tuple(sorted(requester_per_merchant.get(m, []))) for m in merchant_ips.keys()}
    if len(requester_sets) == 1:
        requesters_str = ", ".join(next(iter(requester_sets)))
    else:
        requesters_str = "; ".join(
            f"{m}: {', '.join(requester_per_merchant.get(m, []))}" for m in merchant_ips.keys()
        )
    title = f"open whitelist IP {merchants_str} > api: {domains_str} (requested by {requesters_str})"

    await message.reply_text(
        "```\n" + "\n".join(all_summary) + f"\n\n🌿 Branch: {branch}\n⏳ Đang chạy terragrunt plan...\n```",
        parse_mode="Markdown"
    )

    plan_code, plan_output = await run_cmd_async(
        ["terragrunt", "plan", "-no-color", "--terragrunt-non-interactive", *target_flags],
        cwd=TERRAGRUNT_DIR
    )
    log, has_destroy = filter_plan_output(plan_output, kind="whitelist")

    if has_destroy:
        await run_cmd_async(["git", "checkout", "--", IP_FILE], cwd=repo_dir)
        await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
        await run_cmd_async(["git", "branch", "-D", branch], cwd=repo_dir)
        await message.reply_text(
            f"🚨 *Plan bị huỷ tự động!*\n\n"
            f"Phát hiện có IP bị xoá khỏi whitelist.\n"
            f"Không cho phép apply khi có destroy!\n\n"
            f"```\n{log}\n```",
            parse_mode="Markdown"
        )
        return

    pending[chat_id] = {
        "type": "whitelist",
        "identifier": merchants_str,
        "branch": branch,
        "file": IP_FILE,
        "terragrunt_dir": TERRAGRUNT_DIR,
        "target_flags": target_flags,
        "commit_msg": title,
        "mr_title": title,
        "merchant_ips": merchant_ips,
        "selected_per_merchant": selected_per_merchant,
        "merchant_name_per_merchant": merchant_name_per_merchant,
    }

    status = "✅ Plan thành công" if plan_code == 0 else "❌ Plan failed"
    await message.reply_text(
        f"{status}\n\n```\n{log}\n```\n\nGõ /apply để apply hoặc /cancel để huỷ.",
        parse_mode="Markdown"
    )


def build_checklist_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Mỗi list 1 nút riêng, hiện domain (list_label — dễ hiểu hơn tên kỹ thuật của
    list) + trạng thái tick (☐/✅) ngay trên nút — không cần dò số ngược lại khối
    text phía trên."""
    selected = pending_whitelist[chat_id]["current_selected"]
    buttons = []
    for i, lst in enumerate(MERCHANT_LISTS):
        mark = "✅" if i in selected else "☐"
        label = f"{mark} {list_label(lst)}"
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"wl_toggle:{i}")])
    return InlineKeyboardMarkup(buttons)


def build_list_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Hàng Xác nhận/Huỷ, gửi ở tin nhắn riêng để không bao giờ lẫn với lưới số ở trên."""
    data = pending_whitelist[chat_id]
    current_idx = data["current_idx"]
    total = len(data["queue"])

    if current_idx + 1 >= total:
        confirm_label = "✅ Xác nhận"
    else:
        confirm_label = f"➡️ Tiếp: {data['queue'][current_idx + 1][0]}"
        if len(confirm_label) > 60:
            confirm_label = confirm_label[:57] + "..."

    return InlineKeyboardMarkup([[
        InlineKeyboardButton(confirm_label, callback_data="wl_confirm"),
        InlineKeyboardButton("❌ Huỷ", callback_data="wl_cancel"),
    ]])


async def ask_merchant_name(message, chat_id: int, merchant: str, prefix: str = ""):
    """Hỏi tên công ty đầy đủ (Merchant name) cho merchant — hỏi ngay sau khi gõ
    merchant/IP, trước bước chọn list."""
    awaiting_whitelist_merchant_name.add(chat_id)
    await message.reply_text(
        f"{prefix}🏢 Tên công ty đầy đủ (Merchant name) của `{merchant}`?",
        parse_mode="Markdown",
    )


async def send_list_selection_prompt(message, chat_id: int, merchant: str, prefix: str = ""):
    """Gửi nút chọn list (mỗi list 1 nút riêng, hiện tên đầy đủ + tick ☐/✅ — xem
    build_checklist_keyboard) — tên đã hiện thẳng trên nút nên không cần liệt kê
    danh sách dạng text riêng nữa."""
    pending_whitelist[chat_id]["current_selected"] = set()

    await message.reply_text(
        f"{prefix}📋 Chọn list cho merchant `{merchant}`:\n👆 Bấm để tick, bấm lại để bỏ:",
        parse_mode="Markdown",
        reply_markup=build_checklist_keyboard(chat_id),
    )
    await message.reply_text(
        "👉 Chọn xong thì bấm:",
        reply_markup=build_list_confirm_keyboard(chat_id),
    )


async def whitelist_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id

    if chat_id != ALLOWED_GROUP_ID:
        await query.answer("⛔ Không có quyền.", show_alert=True)
        return

    data = pending_whitelist.get(chat_id)
    if not data:
        await query.answer("⚠️ Yêu cầu đã hết hạn, gõ /whitelist lại.", show_alert=True)
        return

    action = query.data

    try:
        if action == "wl_cancel":
            pending_whitelist.pop(chat_id, None)
            await query.answer()
            await query.edit_message_text("🔄 Đã huỷ chọn list.")
            return

        if action.startswith("wl_toggle:"):
            idx = int(action.split(":")[1])
            selected = data["current_selected"]
            if idx in selected:
                selected.remove(idx)
                toast = f"☐ Bỏ tick #{idx + 1} {list_label(MERCHANT_LISTS[idx])}"
            else:
                selected.add(idx)
                toast = f"✅ Đã tick #{idx + 1} {list_label(MERCHANT_LISTS[idx])}"
            await query.answer(toast)
            await query.edit_message_reply_markup(reply_markup=build_checklist_keyboard(chat_id))
            return

        if action == "wl_confirm":
            if not data["current_selected"]:
                await query.answer("⚠️ Chưa chọn list nào!", show_alert=True)
                return

            await query.answer()
            current_idx = data["current_idx"]
            current_merchant = data["queue"][current_idx][0]
            selected_lists = [MERCHANT_LISTS[i] for i in sorted(data["current_selected"])]

            # Lưu list đã chọn cho merchant hiện tại, giữ nguyên current_idx
            # để hỏi email cho đúng merchant này trước khi sang merchant tiếp theo
            # (Merchant name đã hỏi trước bước chọn list rồi)
            data["selected_per_merchant"][current_merchant] = selected_lists

            awaiting_whitelist_email.add(chat_id)
            selected_names = ', '.join(list_label(l) for l in selected_lists)
            await query.edit_message_text(
                "```\n"
                f"✅ {current_merchant} → {selected_names}\n\n"
                f"👤 Email người yêu cầu whitelist {current_merchant} ?\n"
                "\n```",
                parse_mode="Markdown",
            )

    except Exception as e:
        import traceback
        tb = traceback.format_exc()[-1500:]
        await query.message.reply_text(
            f"🚨 Lỗi:\n```\n{e}\n```\n```\n{tb}\n```",
            parse_mode="Markdown"
        )
