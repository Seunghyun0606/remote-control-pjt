from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from remote_control.approvals.registry import ApprovalPrompt
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
        self.application.add_handler(
            CallbackQueryHandler(self._handle_callback, pattern=r"^approval:")
        )
        self.application.add_handler(MessageHandler(filters.TEXT, self._handle_update))
        self.controller.jobs.set_notifier(self.send_message)
        self.controller.jobs.set_approval_notifier(self.send_approval)

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

    async def send_approval(self, user_id: str, approval: ApprovalPrompt) -> None:
        await self.application.bot.send_message(
            chat_id=int(user_id),
            text=format_approval_message(approval),
            reply_markup=build_approval_markup(approval),
        )

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

    async def _handle_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return

        await query.answer()
        if not is_authorized(user.id, self.allowed_user_ids):
            logger.warning("unauthorized telegram callback rejected: %s", user.id)
            await query.answer("권한이 없습니다.", show_alert=True)
            return

        try:
            approval_id, action, value = parse_approval_callback(query.data or "")
            if action == "details":
                prompt = await self.controller.approval_details(
                    approval_id,
                    user_id=str(user.id),
                )
                await self.application.bot.send_message(
                    chat_id=user.id,
                    text=format_approval_details(prompt),
                )
                return

            if action == "reject":
                response = await self.controller.respond_approval(
                    approval_id,
                    user_id=str(user.id),
                    rejected=True,
                )
            else:
                assert value is not None
                response = await self.controller.respond_approval(
                    approval_id,
                    user_id=str(user.id),
                    option_key=value,
                )
            await query.edit_message_text(response)
        except (KeyError, ValueError) as exc:
            await self.application.bot.send_message(
                chat_id=user.id,
                text=f"Human Gate 응답을 처리할 수 없습니다: {exc}",
            )
        except Exception:
            logger.exception("telegram approval callback failed")
            await self.application.bot.send_message(
                chat_id=user.id,
                text="Human Gate 응답 처리 중 오류가 발생했습니다.",
            )


def build_approval_markup(approval: ApprovalPrompt) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    option_buttons = [
        InlineKeyboardButton(
            text=f"{option.key} · {option.label}"[:60],
            callback_data=f"approval:{approval.id}:choose:{option.key}",
        )
        for option in approval.options
    ]
    for index in range(0, len(option_buttons), 2):
        rows.append(option_buttons[index : index + 2])
    rows.append(
        [
            InlineKeyboardButton(
                text="Details",
                callback_data=f"approval:{approval.id}:details",
            ),
            InlineKeyboardButton(
                text="Reject",
                callback_data=f"approval:{approval.id}:reject",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def parse_approval_callback(data: str) -> tuple[str, str, str | None]:
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "approval":
        raise ValueError("invalid approval callback")
    approval_id = parts[1]
    action = parts[2]
    if action == "choose" and len(parts) == 4 and parts[3]:
        return approval_id, action, parts[3]
    if action in {"details", "reject"} and len(parts) == 3:
        return approval_id, action, None
    raise ValueError("invalid approval callback")


def format_approval_message(approval: ApprovalPrompt) -> str:
    options = "\n".join(
        f"{option.key}. {option.label}" for option in approval.options
    )
    return (
        f"⚠ Human Gate\n\n"
        f"Job: {approval.job_id}\n"
        f"Type: {approval.approval_type}\n\n"
        f"{approval.question}\n\n"
        f"{options}\n\n"
        "선택지를 누르거나 동일한 선택 키를 메시지로 보내세요."
    )


def format_approval_details(approval: ApprovalPrompt) -> str:
    option_details = "\n".join(
        f"- {option.key}: {option.label}"
        + (f" — {option.description}" if option.description else "")
        for option in approval.options
    )
    details = approval.details or "추가 상세 설명이 없습니다."
    return (
        f"Human Gate Details\n\n"
        f"Approval: {approval.id}\n"
        f"Job: {approval.job_id}\n"
        f"Type: {approval.approval_type}\n\n"
        f"Question:\n{approval.question}\n\n"
        f"Details:\n{details}\n\n"
        f"Options:\n{option_details}"
    )


def is_authorized(user_id: int, allowed_user_ids: set[int]) -> bool:
    return user_id in allowed_user_ids
