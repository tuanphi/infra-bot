"""A minimal Telegram interface for the Gitlab Ansible workflow."""

import asyncio
from functools import partial
import logging
import re
import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Python 3\.8 is no longer supported by the Python core team.*",
)

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from config import Settings, load_env_file
from gitlab_access import GitLabAccessWorkflow
from gitlab_conversation import create_gitlab_conversation
from runtime import prepare_repository, start_health_server
from workflow import Pipeline, WorkflowError


class TelegramSafeFormatter(logging.Formatter):
    """Redact Bot API credentials, including those in formatted tracebacks."""

    def format(self, record):
        text = super().format(record)
        return re.sub(
            r"(https?://api\.telegram\.org/(?:file/)?bot)[^/\s\"']+",
            r"\1<redacted>", text, flags=re.IGNORECASE,
        )


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for handler in logging.getLogger().handlers:
    handler.setFormatter(TelegramSafeFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def authorization_status(settings, update):
    chat, user = update.effective_chat, update.effective_user
    message = update.effective_message
    anonymous_sender = message is not None and message.sender_chat is not None
    user_allowed = user is not None and user.id > 0 and user.id in settings.allowed_user_ids
    chat_allowed = chat is not None and (
        (chat.type in ("group", "supergroup") and settings.allowed_group_id < 0
         and chat.id == settings.allowed_group_id)
        or (chat.type == "private" and settings.allow_private_chat)
    )
    reasons = []
    if chat is None or user is None:
        reasons.append("missing_identity")
    if anonymous_sender:
        reasons.append("anonymous_sender")
    if not chat_allowed:
        reasons.append("private_chat_disabled" if chat is not None and chat.type == "private" else "chat_not_allowed")
    if not user_allowed:
        reasons.append("user_not_allowed")
    return {
        "chat_id": chat.id if chat is not None else None,
        "chat_type": chat.type if chat is not None else None,
        "user_id": user.id if user is not None else None,
        "user_allowed": user_allowed,
        "chat_allowed": chat_allowed,
        "reasons": tuple(reasons),
    }


DENIAL_REASONS = {
    "missing_identity": "Không xác định được chat/user gửi lệnh.",
    "anonymous_sender": "Bạn đang gửi dưới danh tính nhóm/kênh; hãy gửi bằng tài khoản cá nhân.",
    "private_chat_disabled": "Tin nhắn riêng chưa được bật (ALLOW_PRIVATE_CHAT=false).",
    "chat_not_allowed": "Chat hiện tại không khớp ALLOWED_GROUP_ID.",
    "user_not_allowed": "user_id của bạn chưa nằm trong ALLOWED_USER_IDS.",
}


def log_access_policy(settings):
    logging.info(
        "Telegram access policy: allowed_group_id=%s allowed_user_count=%s allow_private_chat=%s",
        settings.allowed_group_id, len(settings.allowed_user_ids), settings.allow_private_chat,
    )
    # Keep /whoami available when legacy numeric IDs were configured incorrectly.
    if settings.allowed_group_id >= 0:
        logging.warning("ALLOWED_GROUP_ID should be a negative group ID; send /whoami in the intended group to obtain it.")
    if any(user_id <= 0 for user_id in settings.allowed_user_ids):
        logging.warning("ALLOWED_USER_IDS should contain positive user IDs, not group IDs; use /whoami to obtain the requester ID.")


def build_application(settings):
    pipeline = Pipeline(settings)
    run_lock = asyncio.Lock()

    async def authorized(update: Update):
        status = authorization_status(settings, update)
        if status["reasons"]:
            logging.warning(
                "Telegram access denied: chat_id=%s chat_type=%s user_id=%s reasons=%s",
                status["chat_id"], status["chat_type"], status["user_id"], ",".join(status["reasons"]),
            )
            if update.effective_message is not None:
                await update.effective_message.reply_text(
                    "⛔ Bạn không có quyền dùng bot này.\n"
                    + "\n".join(DENIAL_REASONS[reason] for reason in status["reasons"])
                    + "\nchat_id=%s; user_id=%s\nDùng /whoami để kiểm tra ID."
                    % (status["chat_id"], status["user_id"])
                )
            return False
        return True

    async def whoami_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # Only expose the requester's IDs/status; never change access or run work.
        status = authorization_status(settings, update)
        if update.effective_message is None:
            return
        details = "\n".join(DENIAL_REASONS[reason] for reason in status["reasons"])
        await update.effective_message.reply_text(
            "chat_id=%s\nchat_type=%s\nuser_id=%s\n"
            "user_allowed=%s\nchat_allowed=%s\n%s"
            % (status["chat_id"], status["chat_type"], status["user_id"],
               str(status["user_allowed"]).lower(), str(status["chat_allowed"]).lower(),
               details or "Bạn được phép dùng bot trong chat này.")
        )

    async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if await authorized(update):
            await update.effective_message.reply_text(
                "Dùng /gitlab để chọn namespace/service/user/role và cập nhật quyền Ghub. "
                "Dùng /cancel để dừng trước khi xác nhận.\n"
                "Dùng /whoami để xem chat_id, user_id và quyền truy cập.\n"
                "Dùng /run <source-branch> để chạy playbook GitLab trên branch đã commit "
                "và tạo GitLab MR nếu Ansible thành công."
            )

    async def run_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await authorized(update):
            return
        if len(ctx.args) != 1:
            await update.effective_message.reply_text("Cú pháp: /run <source-branch>")
            return
        if run_lock.locked():
            await update.effective_message.reply_text("Đang có một lượt Ansible chạy; thử lại sau.")
            return

        async with run_lock:
            await update.effective_message.reply_text("Đang lấy source và chạy Ansible...")
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, partial(pipeline.run, ctx.args[0], update.effective_user.id)
                )
            except WorkflowError as exc:
                await update.effective_message.reply_text("❌ " + str(exc)[:3500])
                return
            except Exception:
                logging.exception("Unexpected workflow failure")
                await update.effective_message.reply_text("❌ Lỗi nội bộ; kiểm tra log bot.")
                return
            await update.effective_message.reply_text(
                "✅ Ansible thành công.\nCommit: %s\nMR: %s\n\n%s"
                % (result.commit[:12], result.mr_url, result.recap[:1800])
            )

    app = ApplicationBuilder().token(settings.telegram_token).concurrent_updates(False).build()
    app.add_handler(CommandHandler(["whoami", "id"], whoami_command))
    for handler in create_gitlab_conversation(GitLabAccessWorkflow(settings), run_lock, authorized):
        app.add_handler(handler)
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("run", run_command))
    return app


def main():
    load_env_file("/mnt/secrets/.env", override=False)
    settings = Settings.from_env()
    log_access_policy(settings)
    prepare_repository(settings)
    health = start_health_server(port=settings.health_port)
    try:
        build_application(settings).run_polling()
    finally:
        health.shutdown()
        health.server_close()


if __name__ == "__main__":
    main()
