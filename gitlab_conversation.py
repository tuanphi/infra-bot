"""Telegram /gitlab conversation, isolated by chat, user and keyboard nonce."""

import asyncio
from functools import partial
import logging
import secrets
import warnings

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters
from telegram.warnings import PTBUserWarning

from gitlab_access import Finalization, GitCheckError, MergeRequestInfo, ROLES, USERNAME
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

    def get_session(update, ctx):
        return ctx.user_data.get(SESSION_KEY, {}).get(update.effective_chat.id)

    def pop_session(update, ctx):
        sessions = ctx.user_data.get(SESSION_KEY, {})
        session = sessions.pop(update.effective_chat.id, None)
        if not sessions:
            ctx.user_data.pop(SESSION_KEY, None)
        return session

    async def clear_session(update, ctx):
        session = pop_session(update, ctx)
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
        session = get_session(update, ctx)
        if session and session.get("active"):
            if session.get("stage") in ("decision", "review"):
                await show_pending_menu(update.effective_message, session)
            else:
                await update.effective_message.reply_text("Lượt hiện tại đang xử lý; vui lòng chờ.")
            return ConversationHandler.END
        if run_lock.locked():
            await update.effective_message.reply_text("Repo đang được xử lý hoặc chờ Yes/No; thử lại sau.")
            return ConversationHandler.END
        await clear_session(update, ctx)
        ctx.user_data.setdefault(SESSION_KEY, {})[update.effective_chat.id] = {
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
        get_session(update, ctx).update(namespace=namespace, services=services)
        await update.effective_message.reply_text("Nhập tên service")
        return SERVICE

    async def service_input(update, ctx):
        if not await authorized(update):
            return ConversationHandler.END
        session = get_session(update, ctx)
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
        session = get_session(update, ctx)
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
        session = get_session(update, ctx)
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
            await clear_session(update, ctx)
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

    def final_choices(session):
        state = session["finalization"]
        if state.action == "no":
            return ("no",)
        if state.push_confirmed:
            return ("yes",)
        return ("yes", "no")

    async def show_final_menu(message, session):
        choices = final_choices(session)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                answer.title(), callback_data="gitlab:%s:final:%s" % (session["nonce"], answer),
            ) for answer in choices
        ]])
        text = ("Thử lại %s để hoàn tất lượt hiện tại." % choices[0].title() if len(choices) == 1 else
                "Yes: Tạo merge request đẩy lên nhánh master / No: Huỷ thay đổi")
        menu = await message.reply_text(text, reply_markup=keyboard)
        session["menu_id"] = menu.message_id

    async def show_merge_review(message, session):
        info = session["mr_preview"]
        await reply_full(
            message,
            "Thông tin merge request\nBranch: %s\nTarget: master\nCommit: %s\nCommit message: %s\n\nDifference:\n%s"
            % (info.branch, info.commit, info.message, info.difference),
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(answer.title(), callback_data="gitlab:%s:push:%s" % (session["nonce"], answer))
            for answer in ("yes", "no")
        ]])
        menu = await message.reply_text(
            "Bạn có muốn push branch và tạo merge request không?\nYes: Push và tạo MR / No: Huỷ thay đổi",
            reply_markup=keyboard,
        )
        session.update(menu_id=menu.message_id, stage="review")

    async def show_pending_menu(message, session):
        if session.get("stage") == "review":
            await show_merge_review(message, session)
        else:
            await show_final_menu(message, session)

    async def apply_request(update, ctx, session):
        message = update.effective_message
        request = session["request"]
        # confirmation_input acquired the shared lock before creating this task.
        awaiting_decision = False
        try:
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
            session.update(stage="decision", change=change, recap=result.recap)
            awaiting_decision = True
            try:
                await reply_full(message, "✅ Ansible thành công.\n%s/%s: user %s → %s\n%s"
                                 % (request.namespace, request.service, request.username, request.role, result.recap))
                await show_final_menu(message, session)
            except TelegramError:
                logging.exception("Cannot show final menu; /gitlab can show it again")

        finally:
            # The session reserves the repo while waiting, without a sleeping task.
            if not awaiting_decision:
                try:
                    await clear_session(update, ctx)
                finally:
                    run_lock.release()

    async def finish_request(update, ctx, session, review=None):
        message = update.effective_message
        if review is None and session["finalization"].push_confirmed:
            review = session.get("mr_preview")
        try:
            state = await backend(
                workflow.finalize, session["change"], session["answer"],
                update.effective_user.id, session["recap"], session["finalization"],
                review,
            )
        except Exception as exc:
            session["stage"] = "decision"
            if not isinstance(exc, WorkflowError):
                logging.exception("Unexpected finalization failure")
            try:
                if isinstance(exc, GitCheckError):
                    await reply_git_report(message, exc.report)
                await reply_full(message, "❌ " + (str(exc) if isinstance(exc, WorkflowError)
                                                 else "Lỗi nội bộ; kiểm tra log bot."))
                await show_final_menu(message, session)
            except TelegramError:
                logging.exception("Cannot show retry menu; /gitlab can show it again")
            return
        if isinstance(state, MergeRequestInfo):
            session["mr_preview"] = state
            try:
                await show_merge_review(message, session)
            except TelegramError:
                session["stage"] = "review"
                logging.exception("Cannot show MR review; /gitlab can show it again")
            return
        try:
            if state.action == "yes":
                await reply_full(message, "✅ Đã tạo merge request.\nBranch: %s\nCommit: %s\nMR: %s\nĐã về master và git pull --ff-only."
                                 % (state.branch, state.commit[:12], state.mr_url))
            else:
                await message.reply_text("✅ Đã phục hồi YAML và chạy lại Ansible với cấu hình cũ.\nĐã về master và git pull --ff-only.")
        except TelegramError:
            logging.exception("Finalization completed but Telegram reply failed")
        finally:
            try:
                await clear_session(update, ctx)
            finally:
                run_lock.release()

    async def confirmation_input(update, ctx):
        session = await callback_session(update, ctx)
        if session is None:
            return CONFIRM
        answer = update.callback_query.data.rsplit(":", 1)[1]
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
        if answer == "no":
            await clear_session(update, ctx)
            await update.effective_message.reply_text("Đã dừng luồng.")
        else:
            if run_lock.locked():
                await clear_session(update, ctx)
                await update.effective_message.reply_text("Repo đang được xử lý hoặc chờ Yes/No; thử lại sau.")
                return ConversationHandler.END
            await run_lock.acquire()
            session.update(active=True, stage="applying", menu_id=None, finalization=Finalization())
            ctx.application.create_task(apply_request(update, ctx, session), update=update)
        return ConversationHandler.END

    async def final_input(update, ctx):
        session = await callback_session(update, ctx)
        if session is None or session.get("stage") != "decision":
            return
        answer = update.callback_query.data.rsplit(":", 1)[1]
        if answer not in final_choices(session):
            await update.effective_message.reply_text("Lượt hiện tại chỉ cho phép thử lại lựa chọn đang xử lý.")
            return
        session.update(answer=answer, stage="finishing", menu_id=None)
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logging.exception("Cannot remove final keyboard")
        ctx.application.create_task(finish_request(update, ctx, session), update=update)

    async def push_input(update, ctx):
        session = await callback_session(update, ctx)
        if session is None or session.get("stage") != "review":
            return
        answer = update.callback_query.data.rsplit(":", 1)[1]
        review = session["mr_preview"] if answer == "yes" else None
        session.update(answer=answer, stage="finishing", menu_id=None)
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logging.exception("Cannot remove push confirmation keyboard")
        ctx.application.create_task(finish_request(update, ctx, session, review), update=update)

    async def cancel(update, ctx):
        if await authorized(update):
            session = get_session(update, ctx)
            if session and session.get("active"):
                if session.get("stage") in ("decision", "review"):
                    await show_pending_menu(update.effective_message, session)
                else:
                    await update.effective_message.reply_text("Lượt hiện tại đang xử lý; vui lòng chờ.")
                return ConversationHandler.END
            await clear_session(update, ctx)
            await update.effective_message.reply_text("Đã dừng luồng.")
        return ConversationHandler.END

    async def use_buttons(update, ctx):
        if await authorized(update):
            await update.effective_message.reply_text("Bấm nút role hoặc Yes/No ở tin nhắn của bot; dùng /cancel để dừng.")

    async def stale_callback(update, ctx):
        await update.callback_query.answer("Lựa chọn đã hết hạn hoặc thuộc người khác. Chạy /gitlab để bắt đầu.", show_alert=True)

    text = filters.TEXT & ~filters.COMMAND
    # This flow spans text messages and several inline menus. Track by chat/user;
    # callback_session additionally checks the keyboard nonce and message ID.
    # per_message=True would exclude the text/command steps. Suppress only this
    # expected advisory during construction, leaving other PTB warnings visible.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=PTBUserWarning,
            message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message\..*",
        )
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
    return (
        conversation,
        CallbackQueryHandler(final_input, pattern=r"^gitlab:[0-9a-f]{12}:final:(yes|no)$"),
        CallbackQueryHandler(push_input, pattern=r"^gitlab:[0-9a-f]{12}:push:(yes|no)$"),
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(stale_callback, pattern=r"^gitlab:"),
    )
    