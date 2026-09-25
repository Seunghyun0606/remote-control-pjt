from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from uuid import uuid4

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
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
from remote_control.controller.states import JobState
from remote_control.messaging.base import MessagingProvider
from remote_control.storage.models import TelegramProjectTopicRecord
from remote_control.storage.repositories import (
    TelegramMessageBindingRepository,
    TelegramProjectTopicRepository,
)

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"\bJOB-[A-Za-z0-9-]+\b")
_HEADER_RE = re.compile(r"^\[([^\]]+)\]\n")
_STARTED_PROJECT_RE = re.compile(r"^▶\s+(.+?)\s+작업 시작\s*$", re.MULTILINE)
_DETAIL_PROJECT_RE = re.compile(r"^Project:\s*(\S+)\s*$", re.MULTILINE)
_STEERABLE_STATES = {
    JobState.ASSIGNED,
    JobState.STARTING,
    JobState.RUNNING,
}


@dataclass(slots=True)
class _PendingSteer:
    user_id: str
    project_id: str
    instruction: str
    chat_id: str
    thread_id: int | None
    created_at: float


BOT_COMMANDS = (
    BotCommand("start", "도움말과 Project Topic 동기화"),
    BotCommand("help", "사용 가능한 명령 보기"),
    BotCommand("projects", "등록된 프로젝트 목록"),
    BotCommand("sync", "프로젝트별 Telegram Topic 동기화"),
    BotCommand("run", "프로젝트 Codex 작업 시작"),
    BotCommand("status", "현재 active Job 상태"),
    BotCommand("jobs", "최근 Job 목록"),
    BotCommand("job", "특정 Job 상세 조회"),
    BotCommand("retry", "FAILED/WAITING Job 재시도"),
    BotCommand("sessions", "최근 Project Session 목록"),
    BotCommand("session", "Project Session 조회/new/use"),
    BotCommand("doctor", "Host와 실행환경 진단"),
    BotCommand("pause", "Job 일시정지"),
    BotCommand("resume", "Job 재개"),
    BotCommand("steer", "실행 중인 Job에 추가 지시"),
    BotCommand("stop", "Job 중지"),
)


class TelegramProvider(MessagingProvider):
    def __init__(
        self,
        *,
        token: str,
        allowed_user_ids: set[int],
        controller: ControllerService,
        topics: TelegramProjectTopicRepository | None = None,
        bindings: TelegramMessageBindingRepository | None = None,
        selection_ttl_seconds: int = 300,
    ) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        if not allowed_user_ids:
            raise ValueError("Telegram allowlist must not be empty")
        self.allowed_user_ids = allowed_user_ids
        self.controller = controller
        self.topics = topics
        self.bindings = bindings
        self.selection_ttl_seconds = max(int(selection_ttl_seconds), 1)
        self.application: Application = ApplicationBuilder().token(token).build()
        self.application.add_handler(
            CallbackQueryHandler(self._handle_callback, pattern=r"^approval:")
        )
        self.application.add_handler(
            CallbackQueryHandler(self._handle_job_selection, pattern=r"^jobselect:")
        )
        self.application.add_handler(
            CallbackQueryHandler(self._handle_job_action, pattern=r"^jobaction:")
        )
        self.application.add_handler(MessageHandler(filters.TEXT, self._handle_update))
        self.controller.jobs.set_notifier(self.send_message)
        self.controller.jobs.set_approval_notifier(self.send_approval)
        self._topics_enabled = False
        self._pending_steers: dict[str, _PendingSteer] = {}

    async def start(self) -> None:
        await self.application.initialize()
        await self.application.bot.set_my_commands(BOT_COMMANDS)
        me = await self.application.bot.get_me()
        self._topics_enabled = bool(getattr(me, "has_topics_enabled", False))
        if not self._topics_enabled:
            logger.warning(
                "Telegram private topic mode is disabled; "
                "enable Topics for this bot in BotFather to use project topics"
            )
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def stop(self) -> None:
        if self.application.updater is not None:
            await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()

    async def send_message(self, user_id: str, text: str) -> None:
        project_id = extract_project_id(text)
        job_id = extract_job_id(text)
        thread_id: int | None = None
        if project_id is not None:
            topic = await self._ensure_project_topic(
                user_id=user_id,
                chat_id=user_id,
                project_id=project_id,
            )
            if topic is not None:
                thread_id = topic.message_thread_id

        sent = await self.application.bot.send_message(
            chat_id=int(user_id),
            text=text,
            message_thread_id=thread_id,
        )
        await self._bind_sent_message(
            user_id=user_id,
            chat_id=str(sent.chat_id),
            message_id=sent.message_id,
            message_thread_id=sent.message_thread_id,
            project_id=project_id,
            job_id=job_id,
        )

    async def send_approval(self, user_id: str, approval: ApprovalPrompt) -> None:
        job = await self.controller.jobs.require(approval.job_id)
        topic = await self._ensure_project_topic(
            user_id=user_id,
            chat_id=user_id,
            project_id=job.project_id,
        )
        thread_id = topic.message_thread_id if topic is not None else None
        sent = await self.application.bot.send_message(
            chat_id=int(user_id),
            text=format_approval_message(approval),
            reply_markup=build_approval_markup(approval),
            message_thread_id=thread_id,
        )
        await self._bind_sent_message(
            user_id=user_id,
            chat_id=str(sent.chat_id),
            message_id=sent.message_id,
            message_thread_id=sent.message_thread_id,
            project_id=job.project_id,
            job_id=job.id,
        )

    async def sync_project_topics(self, *, user_id: str, chat_id: str) -> str:
        if self.topics is None:
            return "Telegram Topic 저장소가 활성화되지 않았습니다."
        if not self._topics_enabled:
            return (
                "Telegram private Topic mode가 비활성화되어 있습니다. "
                "BotFather에서 이 Bot의 Topics를 활성화한 뒤 /sync 를 다시 실행하세요."
            )

        created = 0
        updated = 0
        failed: list[str] = []
        for project in self.controller.projects.list():
            try:
                existing = await self.topics.get(
                    user_id=user_id,
                    project_id=project.id,
                )
                topic_name = project.name[:128]
                if existing is None:
                    topic = await self.application.bot.create_forum_topic(
                        chat_id=int(chat_id),
                        name=topic_name,
                    )
                    await self.topics.upsert(
                        user_id=user_id,
                        project_id=project.id,
                        chat_id=chat_id,
                        message_thread_id=topic.message_thread_id,
                        topic_name=topic_name,
                    )
                    created += 1
                    continue

                if existing.topic_name != topic_name:
                    await self.application.bot.edit_forum_topic(
                        chat_id=int(chat_id),
                        message_thread_id=existing.message_thread_id,
                        name=topic_name,
                    )
                    await self.topics.upsert(
                        user_id=user_id,
                        project_id=project.id,
                        chat_id=chat_id,
                        message_thread_id=existing.message_thread_id,
                        topic_name=topic_name,
                    )
                    updated += 1
            except TelegramError as exc:
                logger.warning(
                    "failed to sync Telegram project topic user=%s project=%s: %s",
                    user_id,
                    project.id,
                    exc,
                )
                failed.append(project.id)

        summary = f"Project Topic 동기화 완료: 생성 {created}, 이름갱신 {updated}"
        if failed:
            summary += "\n실패: " + ", ".join(failed)
        return summary

    async def _handle_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        if (
            update.effective_user is None
            or update.effective_message is None
            or update.effective_chat is None
        ):
            return

        user_id_int = update.effective_user.id
        if not is_authorized(user_id_int, self.allowed_user_ids):
            logger.warning("unauthorized telegram user rejected: %s", user_id_int)
            await update.effective_message.reply_text("권한이 없습니다.")
            return

        user_id = str(user_id_int)
        chat_id = str(update.effective_chat.id)
        message = update.effective_message
        thread_id = message.message_thread_id
        text = (message.text or "").strip()
        project_id = await self._project_for_thread(
            chat_id=chat_id,
            message_thread_id=thread_id,
        )

        command = _command_name(text)
        if command == "/sync":
            response = await self.sync_project_topics(
                user_id=user_id,
                chat_id=chat_id,
            )
            await self._send_response(
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                text=response,
                project_id=project_id,
            )
            return

        if command == "/start":
            sync_result = await self.sync_project_topics(
                user_id=user_id,
                chat_id=chat_id,
            )
            help_text = await self.controller.handle_text(
                "/help",
                channel="telegram",
                user_id=user_id,
                project_id=project_id,
            )
            await self._send_response(
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                text=f"{help_text}\n\n{sync_result}",
                project_id=project_id,
            )
            return

        try:
            if (
                text
                and not text.startswith("/")
                and message.reply_to_message is not None
                and self.bindings is not None
            ):
                binding = await self.bindings.get(
                    chat_id=chat_id,
                    message_id=message.reply_to_message.message_id,
                )
                if binding is not None and binding.user_id == user_id:
                    job = await self.controller.jobs.select_for_user(
                        user_id,
                        job_id=binding.job_id,
                        states=_STEERABLE_STATES,
                        project_id=project_id,
                    )
                    await self.controller.jobs.steer(job.id, text)
                    await self._send_response(
                        user_id=user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        text=(
                            f"↪ {job.id} Reply 지시 접수\n"
                            "Reply 대상 Codex session에 적용합니다."
                        ),
                        project_id=job.project_id,
                        job_id=job.id,
                    )
                    return

            if text and not text.startswith("/") and project_id is not None:
                pending = await self.controller.jobs.pending_approvals_for_user(
                    user_id,
                    project_id=project_id,
                )
                if not pending:
                    active = await self.controller.jobs.active_for_user(
                        user_id,
                        project_id=project_id,
                    )
                    steerable = [
                        job
                        for job in active
                        if JobState(job.state) in _STEERABLE_STATES
                    ]
                    if len(steerable) > 1:
                        await self._prompt_job_selection(
                            user_id=user_id,
                            chat_id=chat_id,
                            thread_id=thread_id,
                            project_id=project_id,
                            instruction=text,
                            jobs=steerable,
                        )
                        return

            response = await self.controller.handle_text(
                text,
                channel="telegram",
                user_id=user_id,
                project_id=project_id,
            )
        except (CommandParseError, KeyError, ValueError) as exc:
            response = f"명령을 처리할 수 없습니다: {exc}"
        except Exception:
            logger.exception("telegram command failed")
            response = "명령 처리 중 오류가 발생했습니다."

        response_project = project_id or extract_project_id(response)
        action_markup = (
            build_job_action_markup(response)
            if command in {"/job", "/session"}
            else None
        )
        await self._send_response(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            text=response,
            project_id=response_project,
            job_id=extract_job_id(response),
            reply_markup=action_markup,
        )

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

        if not is_authorized(user.id, self.allowed_user_ids):
            logger.warning("unauthorized telegram callback rejected: %s", user.id)
            await query.answer("권한이 없습니다.", show_alert=True)
            return
        await query.answer()

        try:
            approval_id, action, value = parse_approval_callback(query.data or "")
            if action == "details":
                prompt = await self.controller.approval_details(
                    approval_id,
                    user_id=str(user.id),
                )
                job = await self.controller.jobs.require(prompt.job_id)
                await self._send_response(
                    user_id=str(user.id),
                    chat_id=str(query.message.chat_id) if query.message else str(user.id),
                    thread_id=query.message.message_thread_id if query.message else None,
                    text=format_approval_details(prompt),
                    project_id=job.project_id,
                    job_id=job.id,
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

    async def _handle_job_selection(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        if not is_authorized(user.id, self.allowed_user_ids):
            await query.answer("권한이 없습니다.", show_alert=True)
            return

        try:
            token, job_id = parse_job_selection_callback(query.data or "")
            pending = self._pending_steers.get(token)
            if pending is None:
                await query.answer("선택 시간이 만료되었습니다.", show_alert=True)
                return
            if pending.user_id != str(user.id):
                await query.answer("다른 사용자의 선택입니다.", show_alert=True)
                return
            if self._pending_steer_expired(pending):
                self._pending_steers.pop(token, None)
                await query.answer("선택 시간이 만료되었습니다.", show_alert=True)
                return

            self._pending_steers.pop(token, None)
            project_id = pending.project_id
            instruction = pending.instruction
            chat_id = pending.chat_id
            thread_id = pending.thread_id

            job = await self.controller.jobs.select_for_user(
                str(user.id),
                job_id=job_id,
                states=_STEERABLE_STATES,
                project_id=project_id,
            )
            await self.controller.jobs.steer(job.id, instruction)
            await query.answer("추가 지시를 전달했습니다.")
            if query.message is not None:
                await query.edit_message_text(
                    f"↪ {job.id} 추가 지시 접수\n{instruction[:500]}"
                )
                await self._bind_sent_message(
                    user_id=str(user.id),
                    chat_id=chat_id,
                    message_id=query.message.message_id,
                    message_thread_id=thread_id,
                    project_id=project_id,
                    job_id=job.id,
                )
        except (KeyError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
        except Exception:
            logger.exception("telegram job selection failed")
            await query.answer("Job 선택 처리 중 오류가 발생했습니다.", show_alert=True)

    async def _handle_job_action(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None or query.message is None:
            return
        if not is_authorized(user.id, self.allowed_user_ids):
            await query.answer("권한이 없습니다.", show_alert=True)
            return

        try:
            action, job_id = parse_job_action_callback(query.data or "")
            project_id = await self._project_for_thread(
                chat_id=str(query.message.chat_id),
                message_thread_id=query.message.message_thread_id,
            )
            command = (
                f"/retry {job_id}"
                if action == "retry"
                else f"/resume {job_id}"
            )
            response = await self.controller.handle_text(
                command,
                channel="telegram",
                user_id=str(user.id),
                project_id=project_id,
            )
            await query.answer("요청을 접수했습니다.")
            await self._send_response(
                user_id=str(user.id),
                chat_id=str(query.message.chat_id),
                thread_id=query.message.message_thread_id,
                text=response,
                project_id=project_id or extract_project_id(response),
                job_id=extract_job_id(response),
            )
        except (CommandParseError, KeyError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
        except Exception:
            logger.exception("telegram job action failed")
            await query.answer("Job 작업 처리 중 오류가 발생했습니다.", show_alert=True)

    async def _prompt_job_selection(
        self,
        *,
        user_id: str,
        chat_id: str,
        thread_id: int | None,
        project_id: str,
        instruction: str,
        jobs: list,
    ) -> None:
        self._purge_expired_pending_steers()
        token = uuid4().hex[:10]
        self._pending_steers[token] = _PendingSteer(
            user_id=user_id,
            project_id=project_id,
            instruction=instruction,
            chat_id=chat_id,
            thread_id=thread_id,
            created_at=time.monotonic(),
        )
        buttons = [
            [
                InlineKeyboardButton(
                    text=f"{job.id[-12:]} · {job.state}",
                    callback_data=f"jobselect:{token}:{job.id}",
                )
            ]
            for job in jobs[:8]
        ]
        await self.application.bot.send_message(
            chat_id=int(chat_id),
            message_thread_id=thread_id,
            text=(
                "이 Project에 추가 지시 가능한 Job이 여러 개입니다. "
                "전달할 Job을 선택하세요."
            ),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    def _pending_steer_expired(
        self,
        pending: _PendingSteer,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.monotonic() if now is None else now
        ttl = getattr(self, "selection_ttl_seconds", 300)
        return current - pending.created_at >= ttl

    def _purge_expired_pending_steers(self, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        expired = [
            token
            for token, pending in self._pending_steers.items()
            if self._pending_steer_expired(pending, now=current)
        ]
        for token in expired:
            self._pending_steers.pop(token, None)
        return len(expired)

    async def _project_for_thread(
        self,
        *,
        chat_id: str,
        message_thread_id: int | None,
    ) -> str | None:
        if self.topics is None or message_thread_id is None:
            return None
        record = await self.topics.find_by_thread(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
        )
        return record.project_id if record is not None else None

    async def _ensure_project_topic(
        self,
        *,
        user_id: str,
        chat_id: str,
        project_id: str,
    ) -> TelegramProjectTopicRecord | None:
        if self.topics is None or not self._topics_enabled:
            return None

        existing = await self.topics.get(
            user_id=user_id,
            project_id=project_id,
        )
        if existing is not None:
            return existing

        project = self.controller.projects.get(project_id)
        try:
            topic = await self.application.bot.create_forum_topic(
                chat_id=int(chat_id),
                name=project.name[:128],
            )
        except TelegramError as exc:
            logger.warning(
                "failed to lazily create Telegram topic user=%s project=%s: %s",
                user_id,
                project_id,
                exc,
            )
            return None

        return await self.topics.upsert(
            user_id=user_id,
            project_id=project_id,
            chat_id=chat_id,
            message_thread_id=topic.message_thread_id,
            topic_name=project.name[:128],
        )

    async def _send_response(
        self,
        *,
        user_id: str,
        chat_id: str,
        thread_id: int | None,
        text: str,
        project_id: str | None = None,
        job_id: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        sent = await self.application.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            message_thread_id=thread_id,
            reply_markup=reply_markup,
        )
        await self._bind_sent_message(
            user_id=user_id,
            chat_id=str(sent.chat_id),
            message_id=sent.message_id,
            message_thread_id=sent.message_thread_id,
            project_id=project_id,
            job_id=job_id or extract_job_id(text),
        )

    async def _bind_sent_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        message_id: int,
        message_thread_id: int | None,
        project_id: str | None,
        job_id: str | None,
    ) -> None:
        if (
            self.bindings is None
            or project_id is None
            or job_id is None
        ):
            return
        await self.bindings.upsert(
            chat_id=chat_id,
            message_id=message_id,
            message_thread_id=message_thread_id,
            user_id=user_id,
            project_id=project_id,
            job_id=job_id,
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


def build_job_action_markup(text: str) -> InlineKeyboardMarkup | None:
    job_id = extract_job_id(text)
    if job_id is None:
        return None
    state_match = re.search(r"^(?:Job state|State):\s*(\S+)\s*$", text, re.MULTILINE)
    if state_match is None:
        return None
    state = state_match.group(1).upper()
    if state == "FAILED":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("↻ Retry", callback_data=f"jobaction:retry:{job_id}")]]
        )
    if state == "PAUSED":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶ Resume", callback_data=f"jobaction:resume:{job_id}")]]
        )
    return None


def parse_job_action_callback(data: str) -> tuple[str, str]:
    parts = data.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] != "jobaction"
        or parts[1] not in {"retry", "resume"}
        or not parts[2]
    ):
        raise ValueError("invalid Telegram Job action callback")
    return parts[1], parts[2]


def parse_job_selection_callback(data: str) -> tuple[str, str]:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "jobselect" or not parts[1] or not parts[2]:
        raise ValueError("invalid Telegram Job selection callback")
    return parts[1], parts[2]


def extract_job_id(text: str) -> str | None:
    match = _JOB_ID_RE.search(text)
    return match.group(0) if match is not None else None


def extract_project_id(text: str) -> str | None:
    header = _HEADER_RE.search(text)
    if header is not None:
        return header.group(1).split(" / ", 1)[0].strip()
    started = _STARTED_PROJECT_RE.search(text)
    if started is not None:
        return started.group(1).strip()
    detail = _DETAIL_PROJECT_RE.search(text)
    if detail is not None:
        return detail.group(1).strip()
    return None


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]
    return token.split("@", 1)[0].casefold()


def format_approval_message(approval: ApprovalPrompt) -> str:
    options = "\n".join(
        f"{option.key}. {option.label}" for option in approval.options
    )
    return (
        "⚠ Human Gate\n\n"
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
        "Human Gate Details\n\n"
        f"Approval: {approval.id}\n"
        f"Job: {approval.job_id}\n"
        f"Type: {approval.approval_type}\n\n"
        f"Question:\n{approval.question}\n\n"
        f"Details:\n{details}\n\n"
        f"Options:\n{option_details}"
    )


def is_authorized(user_id: int, allowed_user_ids: set[int]) -> bool:
    return user_id in allowed_user_ids
