from __future__ import annotations

from dataclasses import dataclass

import pytest

from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.messaging.telegram import (
    BOT_COMMANDS,
    TelegramProvider,
    extract_job_id,
    extract_project_id,
)
from remote_control.runners.fake import FakeAgentRunner
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    TelegramMessageBindingRepository,
    TelegramProjectTopicRepository,
)


@dataclass
class _FakeSentMessage:
    chat_id: int
    message_id: int
    message_thread_id: int | None


@dataclass
class _FakeTopic:
    message_thread_id: int


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._message_id = 500
        self._thread_id = 100

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        self._message_id += 1
        return _FakeSentMessage(
            chat_id=int(kwargs["chat_id"]),
            message_id=self._message_id,
            message_thread_id=kwargs.get("message_thread_id"),
        )

    async def create_forum_topic(self, *, chat_id: int, name: str):
        self.calls.append({"create_forum_topic": chat_id, "name": name})
        self._thread_id += 1
        return _FakeTopic(message_thread_id=self._thread_id)

    async def edit_forum_topic(self, **kwargs):
        self.calls.append({"edit_forum_topic": kwargs})
        return True


class _FakeApplication:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeChat:
    id: int


class _FakeMessage:
    def __init__(
        self,
        *,
        text: str,
        message_id: int,
        message_thread_id: int | None,
        reply_to_message=None,
    ) -> None:
        self.text = text
        self.message_id = message_id
        self.message_thread_id = message_thread_id
        self.reply_to_message = reply_to_message

    async def reply_text(self, text: str):
        return text


class _FakeUpdate:
    def __init__(
        self,
        *,
        user_id: int,
        chat_id: int,
        message: _FakeMessage,
    ) -> None:
        self.effective_user = _FakeUser(user_id)
        self.effective_chat = _FakeChat(chat_id)
        self.effective_message = message
        self.callback_query = None


def _provider(
    *,
    controller,
    topics,
    bindings,
    bot,
) -> TelegramProvider:
    provider = TelegramProvider.__new__(TelegramProvider)
    provider.allowed_user_ids = {100}
    provider.controller = controller
    provider.topics = topics
    provider.bindings = bindings
    provider.application = _FakeApplication(bot)
    provider._topics_enabled = True
    provider._pending_steers = {}
    return provider


@pytest.mark.asyncio
async def test_telegram_topic_and_message_binding_repositories(database):
    topics = TelegramProjectTopicRepository(database)
    bindings = TelegramMessageBindingRepository(database)

    saved = await topics.upsert(
        user_id="100",
        project_id="demo",
        chat_id="100",
        message_thread_id=42,
        topic_name="Demo Project",
    )
    assert saved.message_thread_id == 42

    by_project = await topics.get(user_id="100", project_id="demo")
    assert by_project is not None
    assert by_project.topic_name == "Demo Project"

    by_thread = await topics.find_by_thread(
        chat_id="100",
        message_thread_id=42,
    )
    assert by_thread is not None
    assert by_thread.project_id == "demo"

    await bindings.upsert(
        chat_id="100",
        message_id=77,
        message_thread_id=42,
        user_id="100",
        project_id="demo",
        job_id="JOB-DEMO",
    )
    binding = await bindings.get(chat_id="100", message_id=77)
    assert binding is not None
    assert binding.job_id == "JOB-DEMO"


def test_telegram_command_menu_and_message_scope_helpers():
    commands = {command.command for command in BOT_COMMANDS}
    assert {"start", "help", "projects", "sync", "run", "status", "jobs", "steer"} <= commands
    assert extract_job_id("[demo / JOB-20260924-ABC123]\nworking") == "JOB-20260924-ABC123"
    assert extract_project_id("[demo / JOB-20260924-ABC123]\nworking") == "demo"
    assert extract_project_id(
        "▶ demo 작업 시작\nJob: JOB-20260924-ABC123\nHost: desktop-main"
    ) == "demo"


@pytest.mark.asyncio
async def test_sync_project_topics_creates_mapping(project_registry, database):
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)
    topics = TelegramProjectTopicRepository(database)
    bindings = TelegramMessageBindingRepository(database)
    bot = _FakeBot()
    provider = _provider(
        controller=controller,
        topics=topics,
        bindings=bindings,
        bot=bot,
    )

    result = await provider.sync_project_topics(user_id="100", chat_id="100")

    assert "생성 1" in result
    topic = await topics.get(user_id="100", project_id="demo")
    assert topic is not None
    assert topic.topic_name == "Demo Project"
    assert topic.message_thread_id == 101


@pytest.mark.asyncio
async def test_project_topic_bare_run_is_scoped(project_registry, database):
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=EventRepository(database),
        runner=FakeAgentRunner(delay=0.1),
        local_host_id="lightsail-main",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)

    response = await controller.handle_text(
        "/run",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert "▶ demo 작업 시작" in response
    assert "Job: JOB-" in response

    status = await controller.handle_text(
        "/status",
        channel="telegram",
        user_id="100",
        project_id="demo",
    )
    assert "demo" in status

    job = (await manager.list(limit=1))[0]
    await manager.wait_until_idle(job.id)


@pytest.mark.asyncio
async def test_reply_to_bound_message_steers_exact_job(
    project_registry,
    database,
):
    jobs = JobRepository(database)
    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)
    topics = TelegramProjectTopicRepository(database)
    bindings = TelegramMessageBindingRepository(database)
    bot = _FakeBot()
    provider = _provider(
        controller=controller,
        topics=topics,
        bindings=bindings,
        bot=bot,
    )

    await topics.upsert(
        user_id="100",
        project_id="demo",
        chat_id="100",
        message_thread_id=42,
        topic_name="Demo Project",
    )
    job = JobRecord(
        id="JOB-REPLY-DEMO",
        project_id="demo",
        requested_by_channel="telegram",
        requested_by_user="100",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="original",
        state="RUNNING",
    )
    await jobs.add(job)
    await bindings.upsert(
        chat_id="100",
        message_id=700,
        message_thread_id=42,
        user_id="100",
        project_id="demo",
        job_id=job.id,
    )

    replied = _FakeMessage(
        text="progress",
        message_id=700,
        message_thread_id=42,
    )
    incoming = _FakeMessage(
        text="backend만 수정해",
        message_id=701,
        message_thread_id=42,
        reply_to_message=replied,
    )
    update = _FakeUpdate(user_id=100, chat_id=100, message=incoming)

    await provider._handle_update(update, None)

    assert manager._steering[job.id] == ["backend만 수정해"]
    assert any(
        "Reply 지시 접수" in call.get("text", "")
        for call in bot.calls
    )


@pytest.mark.asyncio
async def test_multiple_jobs_in_topic_offer_job_selection(
    project_registry,
    database,
):
    jobs = JobRepository(database)
    manager = JobManager(
        projects=project_registry,
        jobs=jobs,
        events=EventRepository(database),
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
    )
    controller = ControllerService(projects=project_registry, jobs=manager)
    topics = TelegramProjectTopicRepository(database)
    bindings = TelegramMessageBindingRepository(database)
    bot = _FakeBot()
    provider = _provider(
        controller=controller,
        topics=topics,
        bindings=bindings,
        bot=bot,
    )
    await topics.upsert(
        user_id="100",
        project_id="demo",
        chat_id="100",
        message_thread_id=42,
        topic_name="Demo Project",
    )
    for suffix in ("A", "B"):
        await jobs.add(
            JobRecord(
                id=f"JOB-MULTI-{suffix}",
                project_id="demo",
                requested_by_channel="telegram",
                requested_by_user="100",
                requested_host="lightsail-main",
                assigned_host="lightsail-main",
                instruction="work",
                state="RUNNING",
            )
        )

    incoming = _FakeMessage(
        text="다음 작업 계속해",
        message_id=800,
        message_thread_id=42,
    )
    update = _FakeUpdate(user_id=100, chat_id=100, message=incoming)
    await provider._handle_update(update, None)

    selection_calls = [
        call
        for call in bot.calls
        if "전달할 Job을 선택하세요" in call.get("text", "")
    ]
    assert len(selection_calls) == 1
    assert selection_calls[0]["message_thread_id"] == 42
    assert provider._pending_steers
