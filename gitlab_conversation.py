"""Telegram /gitlab conversation, isolated by chat, user and keyboard nonce."""

import asyncio
from functools import partial
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from gitlab_access import GitCheckError, ROLES, USERNAME
from workflow import WorkflowError


NAMESPACE, SERVICE, USER, ROLE, CONFIRM = range(5)
SESSION_KEY = "gitlab_access"


def telegram_chunks(text, limit=3500):
    """Yield all text, respecting Telegram's UTF-16 length limit."""
    while text:
        size = 0
        end = 0
        for character in text:
            units = 2 if ord(character) > 0xFFFF else 1
            if size + units > limit:
                break
            size += units
            end += 1
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline >= 0:
                end = newline + 1
        yield text[:end]
        text = text[end:]


async def reply_full(message, text):
    for chunk in telegram_chunks(text):
        await message.reply_text(chunk, parse_mode=None)


async def reply_git_report(message, report):
    await reply_full(message, "$ git status\n" + report.status)
    await reply_full(message, "$ git diff group_vars/gitlab-ghub\n" + (report.diff or "(không có diff)"))


def create_gitlab_conversation(workflow, run_lock, authorized):
    async def backend(function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, partial(function, *args))

    async def clear_session(ctx):
        session = ctx.user_data.pop(SESSION_KEY, None)
        if session and session.get("menu_id"):
            try:
                await ctx.bot.edit_message_reply_markup(
                    chat_id=session["chat_id"], message_id=session["menu_id"], reply_markup=None,
                )
            except TelegramError:
                pass

    async def start(update, ctx):
        if not await authorized(update):
            return ConversationHandler.END
        await clear_session(ctx)
        if run_lock.locked():
            await update.effective_message.reply_text("Đang có một lượt Ansible chạy; thử lại sau.")
            return ConversationHandler.END
        ctx.user_data[SESSION_KEY] = {
            "nonce": secrets.token_hex(6), "chat_id": update.effective_chat.id,
        }
        await update.effective_message.reply_text("Nhập tên namespace")
        return NAMESPACE

    async def namespace_input(update, ctx):
        if not await authorized(update):
            return ConversationHandler.END
        namespace = update.effective_message.text.strip()
        try:
            services = await backend(workflow.services, namespace)
            if not services:
                raise WorkflowError("Namespace %s chưa có service." % namespace)
        except WorkflowError as exc:
            await reply_full(update.effective_message, "❌ %s\nNhập tên namespace" % exc)
            return NAMESPACE
        ctx.user_data[SESSION_KEY].update(namespace=namespace, services=services)
        await update.effective_message.reply_text("Nhập tên service")
        return SERVICE

    async def service_input(update, ctx):
        if not await authorized(update):
            return ConversationHandler.END
        session = ctx.user_data[SESSION_KEY]
        service = update.effective_message.text.strip()
        if service not in session["services"]:
            await reply_full(update.effective_message,
                             "❌ Service không tồn tại trong namespace %s.\nDanh sách: %s\nNhập tên service"
                             % (session["namespace"], ", ".join(session["services"])))
            return SERVICE
        session["service"] = service
        await update.effective_message.reply_text("Nhập tên user")
        return USER

    async def user_input(update, ctx):
        if not await authorized(update):
            return ConversationHandler.END
        username = update.effective_message.text.strip()
        if not USERNAME.fullmatch(username):
            await update.effective_message.reply_text("Tên user không hợp lệ. Nhập username GitLab, ví dụ tuanpv.")
            return USER
        session = ctx.user_data[SESSION_KEY]
        session["username"] = username
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(role, callback_data="gitlab:%s:role:%s" % (session["nonce"], role))]
            for role in ROLES
        ])
        message = await update.effective_message.reply_text("Chọn role", reply_markup=keyboard)
        session["menu_id"] = message.message_id
        return ROLE

    async def callback_session(update, ctx):
        query = update.callback_query
        if not await authorized(update):
            await query.answer("Bạn không có quyền dùng bot này.", show_alert=True)
            return None
        session = ctx.user_data.get(SESSION_KEY)
        parts = query.data.split(":")
        if (not session or len(parts) != 4 or parts[1] != session["nonce"]
                or query.message.message_id != session.get("menu_id")):
            await query.answer("Lựa chọn đã hết hạn hoặc thuộc người khác.", show_alert=True)
            return None
        await query.answer()
        return session

    async def role_input(update, ctx):
        session = await callback_session(update, ctx)
        if session is None:
            return ROLE
        role = update.callback_query.data.rsplit(":", 1)[1]
        await update.callback_query.edit_message_text("Đã chọn role: " + role)
        try:
            request = await backend(workflow.preview, session["namespace"], session["service"],
                                    session["username"], role)
        except WorkflowError as exc:
            if isinstance(exc, GitCheckError):
                await reply_git_report(update.effective_message, exc.report)
            await reply_full(update.effective_message, "❌ " + str(exc))
            await clear_session(ctx)
            return ConversationHandler.END
        session["request"] = request
        status = ("user %s đang có role %s" % (request.username, ", ".join(request.current_roles))
                  if request.current_roles else "user %s chưa có role" % request.username)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes", callback_data="gitlab:%s:confirm:yes" % session["nonce"]),
            InlineKeyboardButton("No", callback_data="gitlab:%s:confirm:no" % session["nonce"]),
        ]])
        message = await update.effective_message.reply_text(
            "%s\nNamespace: %s\nService: %s\nRole được chọn: %s\n\nBạn có muốn thực hiện thay đổi?"
            % (status, request.namespace, request.service, request.role), reply_markup=keyboard,
        )
        session["menu_id"] = message.message_id
        return CONFIRM

    async def apply_request(message, request):
        if run_lock.locked():
            await message.reply_text("Đang có một lượt Ansible chạy; thử lại bằng /gitlab sau.")
            return
        async with run_lock:
            change = None
            playbook_started = False
            try:
                change = await backend(workflow.prepare, request)
                await reply_git_report(message, change.report)
                await message.reply_text(
                    "Kiểm tra Git hợp lệ. Đang chạy:\nansible-playbook -i nonprod gitlab-repos-ghub.yaml --tags=%s,project_user_access"
                    % request.namespace,
                )
                playbook_started = True
                result = await backend(workflow.run_playbook, change)
            except GitCheckError as exc:
                await reply_git_report(message, exc.report)
                await reply_full(message, "❌ " + str(exc))
                return
            except WorkflowError as exc:
                await reply_full(message, "❌ " + str(exc))
                return
            except Exception:
                logging.exception("Unexpected /gitlab failure")
                if change is not None and not playbook_started:
                    await backend(workflow.rollback, change)
                await message.reply_text("❌ Lỗi nội bộ; kiểm tra log bot.")
                return
            await reply_full(message, "✅ Ansible thành công.\n%s/%s: user %s → %s\n%s\nDiff YAML được giữ để commit."
                             % (request.namespace, request.service, request.username, request.role, result.recap))

    async def confirmation_input(update, ctx):
        session = await callback_session(update, ctx)
        if session is None:
            return CONFIRM
        answer = update.callback_query.data.rsplit(":", 1)[1]
        request = session["request"]
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
        ctx.user_data.pop(SESSION_KEY, None)
        if answer == "no":
            await update.effective_message.reply_text("Đã dừng luồng.")
        else:
            await update.effective_message.reply_text("Đã xác nhận. Đang cập nhật cấu hình...")
            # Keep polling responsive; the shared lock prevents overlapping runs.
            ctx.application.create_task(apply_request(update.effective_message, request), update=update)
        return ConversationHandler.END

    async def cancel(update, ctx):
        if await authorized(update):
            await clear_session(ctx)
            await update.effective_message.reply_text("Đã dừng luồng.")
        return ConversationHandler.END

    async def use_buttons(update, ctx):
        if await authorized(update):
            await update.effective_message.reply_text("Bấm nút role hoặc Yes/No ở tin nhắn của bot; dùng /cancel để dừng.")

    async def stale_callback(update, ctx):
        await update.callback_query.answer("Lựa chọn đã hết hạn hoặc thuộc người khác. Chạy /gitlab để bắt đầu.", show_alert=True)

    text = filters.TEXT & ~filters.COMMAND
    conversation = ConversationHandler(
        entry_points=[CommandHandler("gitlab", start)],
        states={
            NAMESPACE: [MessageHandler(text, namespace_input)],
            SERVICE: [MessageHandler(text, service_input)],
            USER: [MessageHandler(text, user_input)],
            ROLE: [CallbackQueryHandler(role_input, pattern=r"^gitlab:[0-9a-f]{12}:role:(maintainer|developer|reporter|guest)$"),
                   MessageHandler(text, use_buttons)],
            CONFIRM: [CallbackQueryHandler(confirmation_input, pattern=r"^gitlab:[0-9a-f]{12}:confirm:(yes|no)$"),
                      MessageHandler(text, use_buttons)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True, per_chat=True, per_user=True, per_message=False,
    )
    return conversation, CallbackQueryHandler(stale_callback, pattern=r"^gitlab:")
