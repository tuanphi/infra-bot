import os
import re
import asyncio

from telegram import Update, BotCommand, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import (
    TELEGRAM_TOKEN, ALLOWED_GROUP_ID, GIT_BRANCH,
    GOOGLE_SHEETS_ID, GOOGLE_SHEETS_CREDENTIALS_JSON,
)
from state import (
    pending, pending_vpn, pending_whitelist,
    awaiting_whitelist_text, awaiting_whitelist_merchant_name,
    awaiting_whitelist_email, awaiting_vpn_text,
)
from common import (
    guard, run_cmd_async, filter_plan_output, create_gitlab_mr, extract_domain,
    list_label, is_valid_email, is_valid_ip, normalize_email,
)
from sheets import sheet_record_whitelist, translate_sheet_error
from whitelist import (
    cmd_whitelist, process_whitelist_args, whitelist_finalize,
    ask_merchant_name, send_list_selection_prompt, whitelist_callback,
)
from vpn import cmd_vpn, process_vpn_args, vpn_callback
from ocr_whitelist import extract_text, parse_whitelist_form, match_lists_by_domain


# ─── Menu ───────────────────────────────────────────────────────────────────

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["/whitelist", "/vpn"],
        ["/apply", "/cancel"],
        ["/help"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Gõ lệnh hoặc bấm nút bên dưới...",
)


# ─── Handlers chung ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "👋 Chào mừng đến với *GPay Infra Bot*!\n\n"
        "📌 *Lệnh có sẵn:*\n\n"
        "/whitelist\n```\n<email_người_yêu_cầu>\n<merchant> = <ip1> [ip2]...\n```\n"
        "→ Thêm IP whitelist + chạy plan\n\n"
        "/vpn `<email>`\n"
        "→ Cấp quyền VPN — chọn team áp dụng\n\n"
        "/apply\n"
        "→ Apply + commit + push + tạo MR\n\n"
        "/cancel\n"
        "→ Huỷ và revert",
        parse_mode="Markdown",
        reply_markup=MENU_KEYBOARD,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "📋 *Lệnh có sẵn:*\n\n"
        "/whitelist\n```\n<email_người_yêu_cầu>\n<merchant> = <ip1> [ip2]...\n```\n"
        "→ Thêm IP whitelist + chạy plan\n\n"
        "/vpn `<email>`\n"
        "→ Cấp quyền VPN — chọn team áp dụng\n\n"
        "/apply\n"
        "→ Apply + commit + push + tạo MR\n\n"
        "/cancel\n"
        "→ Huỷ và revert\n\n"
        "/help\n"
        "→ Xem lệnh này",
        parse_mode="Markdown",
        reply_markup=MENU_KEYBOARD,
    )


async def handle_plain_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_GROUP_ID:
        return

    if chat_id in awaiting_whitelist_text:
        awaiting_whitelist_text.discard(chat_id)
        raw_args = (update.message.text or "").strip()
        await process_whitelist_args(update, ctx, raw_args)
        return

    if chat_id in awaiting_whitelist_merchant_name:
        data = pending_whitelist.get(chat_id)
        if not data:
            awaiting_whitelist_merchant_name.discard(chat_id)
            return

        merchant_name = (update.message.text or "").strip()
        if not merchant_name:
            await update.message.reply_text("❌ Tên công ty không được để trống, gõ lại:")
            return

        awaiting_whitelist_merchant_name.discard(chat_id)
        current_idx = data["current_idx"]
        current_merchant = data["queue"][current_idx][0]
        data.setdefault("merchant_name_per_merchant", {})[current_merchant] = merchant_name

        # merchant_name là text tự do người dùng gõ — KHÔNG interpolate trực tiếp vào
        # message có parse_mode="Markdown" (có thể chứa _, *, ` phá định dạng).
        await send_list_selection_prompt(
            update.message, chat_id, current_merchant,
            prefix="✅ Đã lưu tên công ty.\n\n",
        )
        return

    if chat_id in awaiting_whitelist_email:
        data = pending_whitelist.get(chat_id)
        if not data:
            awaiting_whitelist_email.discard(chat_id)
            return

        raw_emails = re.split(r'[,\s]+', (update.message.text or "").strip())
        raw_emails = [e for e in raw_emails if e]
        # Gõ tắt không có @ -> tự thêm @g-pay.vn (vd "hieudd" -> "hieudd@g-pay.vn")
        raw_emails = [normalize_email(e) for e in raw_emails]
        invalid = [e for e in raw_emails if not is_valid_email(e)]
        if invalid or not raw_emails:
            await update.message.reply_text(
                "❌ Email không hợp lệ: `" + "`, `".join(invalid or ["(trống)"]) + "`\n"
                "Gõ lại email người yêu cầu whitelist (có thể nhập nhiều, cách nhau bởi dấu phẩy/khoảng trắng):",
                parse_mode="Markdown"
            )
            return

        awaiting_whitelist_email.discard(chat_id)
        current_idx = data["current_idx"]
        current_merchant = data["queue"][current_idx][0]
        data.setdefault("requester_per_merchant", {})[current_merchant] = raw_emails
        data["current_idx"] += 1

        if data["current_idx"] < len(data["queue"]):
            next_merchant = data["queue"][data["current_idx"]][0]
            done_emails = ', '.join(f"`{e}`" for e in raw_emails)
            await ask_merchant_name(
                update.message, chat_id, next_merchant,
                prefix=f"👤 `{current_merchant}` → {done_emails}\n\n",
            )
        else:
            merchant_ips = data["merchant_ips"]
            selected_per_merchant = data["selected_per_merchant"]
            requester_per_merchant = data["requester_per_merchant"]
            merchant_name_per_merchant = data.get("merchant_name_per_merchant", {})
            pending_whitelist.pop(chat_id, None)

            summary_lines = "\n".join(
                f"• {m} → {', '.join(list_label(l) for l in selected_per_merchant[m])} "
                f"(yêu cầu bởi {', '.join(requester_per_merchant.get(m, []))})"
                for m in merchant_ips.keys()
            )
            await update.message.reply_text(
                "✅ Đã chọn list + email cho tất cả merchant:\n```\n" + summary_lines + "\n```",
                parse_mode="Markdown"
            )
            await whitelist_finalize(
                update.message, merchant_ips, selected_per_merchant,
                requester_per_merchant, merchant_name_per_merchant,
            )
        return

    if chat_id in awaiting_vpn_text:
        awaiting_vpn_text.discard(chat_id)
        raw_emails = (update.message.text or "").split()
        await process_vpn_args(update, ctx, raw_emails)
        return


# ─── Đọc ảnh form đề nghị whitelist (OCR local, không gửi ảnh ra ngoài) ──────

async def handle_whitelist_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_GROUP_ID:
        return
    if chat_id not in awaiting_whitelist_text:
        # Ảnh gửi ngoài luồng /whitelist (không phải đang chờ nhập merchant/IP)
        # -> bỏ qua, đỡ tốn công OCR cho ảnh không liên quan trong group.
        return

    awaiting_whitelist_text.discard(chat_id)
    await update.message.reply_text("🔎 Đang đọc ảnh...")

    try:
        # Ưu tiên "Document" (ảnh gửi dạng file, Telegram KHÔNG nén lại) nếu có —
        # OCR đọc ảnh nén (kiểu "Photo" thường) hay bị sai dấu/số do mất nét chữ.
        if update.message.document:
            tg_file = await update.message.document.get_file()
        else:
            tg_file = await update.message.photo[-1].get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())

        try:
            text = extract_text(image_bytes)
        except Exception as e:
            await update.message.reply_text(f"❌ Không đọc được ảnh: {e}")
            return

        data = parse_whitelist_form(text)

        requester_email = normalize_email(data["requester_email"]) if data["requester_email"] else ""
        merchant_code = data["merchant_code"]
        ips = data["ips"]

        missing = []
        if not merchant_code:
            missing.append("Merchant Code")
        if not ips:
            missing.append("IP của Merchant")
        if not requester_email or not is_valid_email(requester_email):
            missing.append("Email người đề nghị")
        if missing:
            await update.message.reply_text(
                "⚠️ Đọc ảnh không đủ/không rõ thông tin: " + ", ".join(missing) + "\n"
                "Gõ tay bằng `/whitelist` nhé.\n\n"
                "Text OCR đọc được (để đối chiếu):\n```\n" + (text.strip()[:800] or "(rỗng)") + "\n```",
                parse_mode="Markdown",
            )
            return

        invalid_ips = [ip for ip in ips if not is_valid_ip(ip)]
        if invalid_ips:
            await update.message.reply_text(
                "❌ IP đọc được không hợp lệ: `" + "`, `".join(invalid_ips) + "`\n"
                "Gõ tay bằng `/whitelist` nhé.",
                parse_mode="Markdown",
            )
            return

        matched_lists = match_lists_by_domain(data["domain_raw"])
        if not matched_lists:
            await update.message.reply_text(
                "⚠️ Không khớp được Domain API truy cập với list nào đang cấu hình.\n"
                f"Domain đọc được: {data['domain_raw'] or '(không đọc được)'}\n"
                "Gõ tay bằng `/whitelist` nhé.",
                parse_mode="Markdown",
            )
            return

        merchant_name = data["merchant_name"] or merchant_code
        merchant_ips = {merchant_code: ips}
        pending_whitelist[chat_id] = {
            "merchant_ips": merchant_ips,
            "requester_per_merchant": {merchant_code: [requester_email]},
            "merchant_name_per_merchant": {merchant_code: merchant_name},
            "queue": list(merchant_ips.items()),
            "current_idx": 0,
            "selected_per_merchant": {merchant_code: matched_lists},
            "from_ocr": True,
        }

        list_names = ', '.join(list_label(l) for l in matched_lists)
        await update.message.reply_text(
            "📸 *Đã đọc được từ ảnh:*\n"
            f"• Merchant: `{merchant_code}` ({merchant_name})\n"
            f"• IP: {', '.join(ips)}\n"
            f"• Requester: {requester_email}\n"
            f"• List: {list_names}\n\n"
            "⚠️ Kiểm tra kỹ trước khi chạy plan — OCR có thể đọc nhầm số/chữ.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Đúng rồi, chạy plan", callback_data="wlocr_confirm"),
                InlineKeyboardButton("❌ Sai, huỷ", callback_data="wlocr_cancel"),
            ]]),
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()[-1500:]
        await update.message.reply_text(
            f"🚨 Lỗi khi xử lý ảnh:\n```\n{e}\n```\n```\n{tb}\n```",
            parse_mode="Markdown"
        )


async def whitelist_ocr_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id

    if chat_id != ALLOWED_GROUP_ID:
        await query.answer("⛔ Không có quyền.", show_alert=True)
        return

    data = pending_whitelist.get(chat_id)
    if not data or not data.get("from_ocr"):
        await query.answer("⚠️ Không có dữ liệu đọc từ ảnh đang chờ.", show_alert=True)
        return

    await query.answer()

    if query.data == "wlocr_cancel":
        pending_whitelist.pop(chat_id, None)
        await query.edit_message_text("🔄 Đã huỷ.")
        return

    merchant_ips = data["merchant_ips"]
    selected_per_merchant = data["selected_per_merchant"]
    requester_per_merchant = data["requester_per_merchant"]
    merchant_name_per_merchant = data["merchant_name_per_merchant"]
    pending_whitelist.pop(chat_id, None)

    await query.edit_message_text("🚀 Đang xử lý...")
    await whitelist_finalize(
        query.message, merchant_ips, selected_per_merchant,
        requester_per_merchant, merchant_name_per_merchant,
    )


# ─── /apply, /cancel ────────────────────────────────────────────────────────

async def cmd_apply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    data = pending.get(update.effective_chat.id)
    if not data:
        await update.message.reply_text(
            "⚠️ Không có plan nào đang chờ. Chạy `/whitelist` hoặc `/vpn` trước.",
            parse_mode="Markdown"
        )
        return

    branch          = data["branch"]
    file_path       = data["file"]
    terragrunt_dir  = data["terragrunt_dir"]
    commit_msg      = data["commit_msg"]
    mr_title        = data["mr_title"]
    repo_dir        = os.path.dirname(file_path)

    await update.message.reply_text("🚀 Đang apply...")

    # Dùng -target để tránh refresh toàn bộ state
    if data.get("type") == "whitelist":
        target_flag = data.get("target_flags") or ['-target=cloudflare_list.this["merchant_ips"]']
    else:
        target_flag = []

    apply_code, apply_output = await run_cmd_async(
        ["terragrunt", "apply", "-auto-approve", "-no-color", "--terragrunt-non-interactive", *target_flag],
        cwd=terragrunt_dir
    )
    log, _ = filter_plan_output(apply_output, kind=data.get("type", "whitelist"))

    if apply_code != 0:
        await update.message.reply_text(
            f"❌ Apply failed:\n```\n{log}\n```",
            parse_mode="Markdown"
        )
        return

    await run_cmd_async(["git", "add", file_path], cwd=repo_dir)
    await run_cmd_async(["git", "commit", "-m", commit_msg], cwd=repo_dir)
    push_code, push_out = await run_cmd_async(["git", "push", "origin", branch], cwd=repo_dir)

    if push_code != 0:
        await update.message.reply_text(
            f"✅ Apply xong nhưng git push lỗi:\n```\n{push_out}\n```",
            parse_mode="Markdown"
        )
        return

    pending.pop(update.effective_chat.id, None)

    await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
    await run_cmd_async(["git", "branch", "-D", branch], cwd=repo_dir)
    await asyncio.sleep(2)
    mr_result = create_gitlab_mr(branch, mr_title)

    # Ghi log whitelist vào Google Sheet — chỉ sau khi apply thành công thật sự.
    # Best-effort: lỗi ghi sheet chỉ báo, không coi apply là fail (infra đã live rồi).
    sheet_note = ""
    if data.get("type") == "whitelist" and GOOGLE_SHEETS_ID and GOOGLE_SHEETS_CREDENTIALS_JSON:
        sheet_lines = []
        for merchant, ips in data.get("merchant_ips", {}).items():
            merchant_name = data.get("merchant_name_per_merchant", {}).get(merchant) or merchant
            lists = data.get("selected_per_merchant", {}).get(merchant, [])
            merchant_domains = sorted({
                extract_domain(lst.get("desc", "")) for lst in lists if extract_domain(lst.get("desc", ""))
            })
            try:
                res = await sheet_record_whitelist(merchant, merchant_name, ips, merchant_domains)
                sheet_lines.append(f"• `{merchant}`: {res}")
            except Exception as e:
                print(f"[SHEET] Lỗi ghi sheet cho merchant {merchant}: {e}", flush=True)
                sheet_lines.append(f"• `{merchant}`: ⚠️ {translate_sheet_error(e)}")
        if sheet_lines:
            sheet_note = "\n\n📊 *Google Sheet:*\n" + "\n".join(sheet_lines)

    await update.message.reply_text(
        f"✅ Apply xong!\n"
        f"🌿 Branch {branch} đã push lên GitLab.\n"
        f"🔀 [View Merge Request]({mr_result})"
        f"{sheet_note}",
        parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    chat_id = update.effective_chat.id
    had_selection = bool(pending_vpn.pop(chat_id, None)) or bool(pending_whitelist.pop(chat_id, None))
    data = pending.pop(chat_id, None)
    if not data:
        await update.message.reply_text("🔄 Đã huỷ." if had_selection else "Không có gì để huỷ.")
        return

    file_path = data["file"]
    branch    = data["branch"]
    repo_dir  = os.path.dirname(file_path)

    await run_cmd_async(["git", "checkout", "--", file_path], cwd=repo_dir)
    await run_cmd_async(["git", "checkout", GIT_BRANCH], cwd=repo_dir)
    await run_cmd_async(["git", "branch", "-D", branch], cwd=repo_dir)

    # Không cần đụng Google Sheet ở đây — giờ chỉ ghi sheet sau khi /apply thành công,
    # nên lúc /cancel sheet chưa hề bị ghi gì, không có gì phải hoàn tác.
    await update.message.reply_text(
        f"🔄 Đã huỷ và revert về `{GIT_BRANCH}`.\nBranch `{branch}` đã bị xoá.",
        parse_mode="Markdown"
    )


# ─── Main ───────────────────────────────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("whitelist", "Thêm IP whitelist + chạy plan"),
        BotCommand("vpn", "Cấp quyền VPN — chọn team áp dụng"),
        BotCommand("apply", "Apply + commit + push + tạo MR"),
        BotCommand("cancel", "Huỷ và revert"),
        BotCommand("help", "Xem hướng dẫn sử dụng"),
    ])


if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("vpn",       cmd_vpn))
    app.add_handler(CommandHandler("apply",     cmd_apply))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CallbackQueryHandler(whitelist_callback, pattern="^wl_"))
    app.add_handler(CallbackQueryHandler(vpn_callback, pattern="^vpn_"))
    app.add_handler(CallbackQueryHandler(whitelist_ocr_callback, pattern="^wlocr_"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_whitelist_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))
    print("🤖 Bot đang chạy...")
    app.run_polling()
