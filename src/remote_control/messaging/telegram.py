from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from remote_control.controller.command_router import CommandParseError
from remote_control.controller.service import ControllerService
from remote_control.messaging.base import MessagingProvider

logger = logging.getLogger(__name__)


class TelegramProvider(MessagingProvider):
    def __init__(
        self,
        *,
        token: str,
        allowed_user_ids: set[int],
        controller: ControllerService,
    ) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        if not allowed_user_ids:
            raise ValueError("Telegram allowlist must not be empty")
        self.allowed_user_ids = allowed_user_ids
        self.controller = controller
        self.application: Application = ApplicationBuilder().token(token).build()
        self.application.add_handler(MessageHandler(filters.TEXT, self._handle_update))
        self.controller.jobs.set_notifier(self.send_message)

    async def start(self) -> None:
        await self.application.initialize()
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def stop(self) -> None:
        if self.application.updater is not None:
            await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()

    async def send_message(self, user_id: str, text: str) -> None:
        await self.application.bot.send_message(chat_id=int(user_id), text=text)

    async def _handle_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        if update.effective_user is None or update.effective_message is None:
            return

        user_id = update.effective_user.id
        if not is_authorized(user_id, self.allowed_user_ids):
            logger.warning("unauthorized telegram user rejected: %s", user_id)
            await update.effective_message.reply_text("권한이 없습니다.")
            return

        text = update.effective_message.text or ""
        try:
            response = await self.controller.handle_text(
                text,
                channel="telegram",
                user_id=str(user_id),
            )
        except (CommandParseError, KeyError, ValueError) as exc:
            response = f"명령을 처리할 수 없습니다: {exc}"
        except Exception:
            logger.exception("telegram command failed")
            response = "명령 처리 중 오류가 발생했습니다."

        await update.effective_message.reply_text(response)


def is_authorized(user_id: int, allowed_user_ids: set[int]) -> bool:
    return user_id in allowed_user_ids
