"""A minimal Telegram interface for the Gitlab Ansible workflow."""

import asyncio
import logging
import os
import re
import warnings
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings(
    "ignore",
    message=r"Python 3\.8 is no longer supported by the Python core team.*",
)

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from gitlab_access import GitLabAccessWorkflow
from gitlab_conversation import create_gitlab_conversation
from runtime import prepare_repository, start_health_server


BOT_COMMANDS = (
    BotCommand("gitlab", "🔐 Phân quyền repo GitLab"),
    BotCommand("cancle", "🛑 Huỷ các lượt đang gom trước khi chạy Ansible"),
    BotCommand("reset", "♻️ Lấy master và đồng bộ quyền namespace"),
    BotCommand("help", "❓ Hướng dẫn sử dụng bot"),
)


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
    run_lock = asyncio.Lock()

    async def register_commands(application):
        await application.bot.set_my_commands(BOT_COMMANDS)
        if settings.allowed_group_id < 0:
            await application.bot.set_my_commands(
                BOT_COMMANDS, scope=BotCommandScopeChat(settings.allowed_group_id),
            )

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
                "Dùng /gitlab để chọn ghub/non-ghub, project/service bằng nút, nhập danh sách user và chọn role chung. "
                "Dùng /reset để chọn nhóm/namespace, lấy master và chạy lại quyền theo YAML.\n"
                "Sau mỗi lượt /gitlab: Yes thêm lượt mới; No chạy Ansible cho các lượt đã gom.\n"
                "Dùng /cancle (hoặc /cancel) để huỷ các lượt đang gom trước Ansible.\n"
                "Dùng /help để xem hướng dẫn.\n"
                "Dùng /whoami để xem chat_id, user_id và quyền truy cập."
            )

    app = (ApplicationBuilder().token(settings.telegram_token).concurrent_updates(False)
           .post_init(register_commands).build())
    app.add_handler(CommandHandler(["whoami", "id"], whoami_command))
    for handler in create_gitlab_conversation(GitLabAccessWorkflow(settings), run_lock, authorized):
        app.add_handler(handler)
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    return app


def main():
    env_values = {}
    for raw in Path("/mnt/secrets/.env").read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, value = re.sub(r"^export\s+", "", line).split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        env_values[name.strip()] = value
    for name, value in env_values.items():
        os.environ.setdefault(name, value)
    settings = SimpleNamespace(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        allowed_group_id=int(os.environ["ALLOWED_GROUP_ID"]),
        allowed_user_ids=frozenset(int(item.strip()) for item in os.environ["ALLOWED_USER_IDS"].split(",") if item.strip()),
        git_repo_url=os.environ["GIT_REPO_URL"],
        git_target_branch=os.environ["GIT_TARGET_BRANCH"],
        gitlab_url=os.environ["GITLAB_URL"].rstrip("/"),
        gitlab_token=os.environ["GITLAB_TOKEN"],
        gitlab_username=os.getenv("GITLAB_USERNAME", "").strip(),
        allow_private_chat=os.getenv("ALLOW_PRIVATE_CHAT", "false").strip().lower() in ("true", "1"),
        ansible_binary=os.getenv("ANSIBLE_PLAYBOOK_BIN", "ansible-playbook"),
        ansible_timeout_seconds=int(os.getenv("ANSIBLE_TIMEOUT_SECONDS", "1800")),
        git_clone_dir=os.getenv("GIT_CLONE_DIR", "/app/infra").strip(),
        git_clone_timeout_seconds=int(os.getenv("GIT_CLONE_TIMEOUT_SECONDS", "180")),
        health_port=int(os.getenv("HEALTH_PORT", "8080")),
    )
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
