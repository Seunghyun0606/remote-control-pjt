from __future__ import annotations

import logging
from typing import Any

from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from remote_control.approvals.registry import ApprovalPrompt
from remote_control.controller.command_router import CommandParseError
from remote_control.controller.service import ControllerService
from remote_control.messaging.base import MessagingProvider

logger = logging.getLogger(__name__)


class SlackProvider(MessagingProvider):
    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        allowed_user_ids: set[str],
        controller: ControllerService,
    ) -> None:
        if not bot_token:
            raise ValueError("Slack bot token is required")
        if not app_token:
            raise ValueError("Slack app token is required")
        if not allowed_user_ids:
            raise ValueError("Slack allowlist must not be empty")

        self.allowed_user_ids = allowed_user_ids
        self.controller = controller
        self.app = AsyncApp(token=bot_token)
        self.handler = AsyncSocketModeHandler(self.app, app_token)
        self._dm_channels: dict[str, str] = {}

        self.app.event("message")(self._handle_message)
        self.app.action("approval_choose")(self._handle_approval_choose)
        self.app.action("approval_details")(self._handle_approval_details)
        self.app.action("approval_reject")(self._handle_approval_reject)

        self.controller.jobs.set_notifier(self.send_message, channel="slack")
        self.controller.jobs.set_approval_notifier(self.send_approval, channel="slack")

    async def start(self) -> None:
        await self.handler.connect_async()

    async def stop(self) -> None:
        await self.handler.close_async()

    async def send_message(self, user_id: str, text: str) -> None:
        channel = await self._dm_channel(user_id)
        await self.app.client.chat_postMessage(channel=channel, text=text)

    async def send_approval(self, user_id: str, approval: ApprovalPrompt) -> None:
        channel = await self._dm_channel(user_id)
        await self.app.client.chat_postMessage(
            channel=channel,
            text=format_approval_message(approval),
            blocks=build_approval_blocks(approval),
        )

    async def _dm_channel(self, user_id: str) -> str:
        cached = self._dm_channels.get(user_id)
        if cached:
            return cached
        result = await self.app.client.conversations_open(users=user_id)
        channel = result.get("channel") if isinstance(result, dict) else None
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            raise RuntimeError(f"Slack DM channel could not be opened for {user_id}")
        self._dm_channels[user_id] = channel_id
        return channel_id

    async def _handle_message(self, body: dict, event: dict, say) -> None:
        del body
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = str(event.get("user") or "")
        if not is_authorized(user_id, self.allowed_user_ids):
            logger.warning("unauthorized slack user rejected: %s", user_id)
            await say("권한이 없습니다.")
            return

        text = str(event.get("text") or "").strip()
        if not text:
            return
        try:
            response = await self.controller.handle_text(
                text,
                channel="slack",
                user_id=user_id,
            )
        except (CommandParseError, KeyError, ValueError) as exc:
            response = f"명령을 처리할 수 없습니다: {exc}"
        except Exception:
            logger.exception("slack command failed")
            response = "명령 처리 중 오류가 발생했습니다."
        await say(response)

    async def _handle_approval_choose(self, ack, body: dict, respond) -> None:
        await ack()
        user_id = _body_user_id(body)
        if not is_authorized(user_id, self.allowed_user_ids):
            await respond("권한이 없습니다.", replace_original=False)
            return
        try:
            approval_id, option_key = parse_approval_value(_action_value(body))
            response = await self.controller.respond_approval(
                approval_id,
                user_id=user_id,
                option_key=option_key,
            )
            await respond(response, replace_original=True)
        except (KeyError, ValueError) as exc:
            await respond(
                f"Human Gate 응답을 처리할 수 없습니다: {exc}",
                replace_original=False,
            )

    async def _handle_approval_details(self, ack, body: dict, respond) -> None:
        await ack()
        user_id = _body_user_id(body)
        if not is_authorized(user_id, self.allowed_user_ids):
            await respond("권한이 없습니다.", replace_original=False)
            return
        try:
            approval_id = _action_value(body)
            prompt = await self.controller.approval_details(
                approval_id,
                user_id=user_id,
            )
            await respond(format_approval_details(prompt), replace_original=False)
        except (KeyError, ValueError) as exc:
            await respond(
                f"Human Gate 상세정보를 조회할 수 없습니다: {exc}",
                replace_original=False,
            )

    async def _handle_approval_reject(self, ack, body: dict, respond) -> None:
        await ack()
        user_id = _body_user_id(body)
        if not is_authorized(user_id, self.allowed_user_ids):
            await respond("권한이 없습니다.", replace_original=False)
            return
        try:
            approval_id = _action_value(body)
            response = await self.controller.respond_approval(
                approval_id,
                user_id=user_id,
                rejected=True,
            )
            await respond(response, replace_original=True)
        except (KeyError, ValueError) as exc:
            await respond(
                f"Human Gate 거절을 처리할 수 없습니다: {exc}",
                replace_original=False,
            )


def build_approval_blocks(approval: ApprovalPrompt) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": format_approval_message(approval),
            },
        }
    ]
    option_elements = [
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": f"{option.key} · {option.label}"[:75],
            },
            "action_id": "approval_choose",
            "value": encode_approval_value(approval.id, option.key),
        }
        for option in approval.options
    ]
    if option_elements:
        blocks.append({"type": "actions", "elements": option_elements[:5]})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Details"},
                    "action_id": "approval_details",
                    "value": approval.id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "approval_reject",
                    "value": approval.id,
                },
            ],
        }
    )
    return blocks


def encode_approval_value(approval_id: str, option_key: str) -> str:
    if "|" in approval_id or "|" in option_key:
        raise ValueError("approval ids and option keys must not contain '|'")
    return f"{approval_id}|{option_key}"


def parse_approval_value(value: str) -> tuple[str, str]:
    approval_id, separator, option_key = value.partition("|")
    if not separator or not approval_id or not option_key:
        raise ValueError("invalid Slack approval value")
    return approval_id, option_key


def format_approval_message(approval: ApprovalPrompt) -> str:
    options = "\n".join(
        f"• *{option.key}* — {option.label}" for option in approval.options
    )
    return (
        f"⚠ *Human Gate*\n"
        f"Job: `{approval.job_id}`\n"
        f"Type: `{approval.approval_type}`\n\n"
        f"{approval.question}\n\n"
        f"{options}"
    )


def format_approval_details(approval: ApprovalPrompt) -> str:
    option_details = "\n".join(
        f"• *{option.key}*: {option.label}"
        + (f" — {option.description}" if option.description else "")
        for option in approval.options
    )
    return (
        f"*Human Gate Details*\n"
        f"Approval: `{approval.id}`\n"
        f"Job: `{approval.job_id}`\n\n"
        f"*Question*\n{approval.question}\n\n"
        f"*Details*\n{approval.details or '추가 상세 설명이 없습니다.'}\n\n"
        f"*Options*\n{option_details}"
    )


def is_authorized(user_id: str, allowed_user_ids: set[str]) -> bool:
    return user_id in allowed_user_ids


def _body_user_id(body: dict) -> str:
    user = body.get("user")
    if not isinstance(user, dict):
        return ""
    return str(user.get("id") or "")


def _action_value(body: dict) -> str:
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Slack action payload is missing actions")
    first = actions[0]
    if not isinstance(first, dict):
        raise ValueError("Slack action payload is invalid")
    value = first.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError("Slack action value is missing")
    return value
