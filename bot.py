"""A minimal Telegram interface for the Gitlab Ansible workflow."""

import asyncio
from functools import partial
import logging
import warnings

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from config import Settings, load_env_file
from workflow import Pipeline, WorkflowError


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

warnings.filterwarnings(
    "ignore",
    message=r"Python 3\.8 is no longer supported by the Python core team.*",
)

def build_application(settings):
    pipeline = Pipeline(settings)
    run_lock = asyncio.Lock()

    async def authorized(update: Update):
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return False
        if chat.id != settings.allowed_group_id or user.id not in settings.allowed_user_ids:
            await update.effective_message.reply_text("⛔ Bạn không có quyền dùng bot này.")
            return False
        return True

    async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if await authorized(update):
            await update.effective_message.reply_text(
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
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("run", run_command))
    return app


if __name__ == "__main__":
    load_env_file("/mnt/secrets/.env", override=False)
    build_application(Settings.from_env()).run_polling()
