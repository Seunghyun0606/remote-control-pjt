from remote_control.controller.command_router import CommandRouter, Intent


def test_parse_run_command(project_registry):
    command = CommandRouter(project_registry).parse("/run demo --host lightsail-main")
    assert command.intent == Intent.RUN_PROJECT
    assert command.project_id == "demo"
    assert command.host == "lightsail-main"


def test_parse_natural_korean(project_registry):
    command = CommandRouter(project_registry).parse("Demo Project 다음 작업 진행해")
    assert command.intent == Intent.RUN_PROJECT
    assert command.project_id == "demo"


def test_natural_text_becomes_agent_steering_not_shell(project_registry):
    command = CommandRouter(project_registry).parse("rm -rf /")
    assert command.intent == Intent.STEER
    assert command.instruction == "rm -rf /"


def test_pause_resume_and_steer_commands(project_registry):
    router = CommandRouter(project_registry)

    pause = router.parse("/pause JOB-1")
    assert pause.intent == Intent.PAUSE
    assert pause.job_id == "JOB-1"

    resume = router.parse("/resume JOB-1")
    assert resume.intent == Intent.RESUME
    assert resume.job_id == "JOB-1"

    steer = router.parse("/steer --job JOB-1 UI는 건드리지 마")
    assert steer.intent == Intent.STEER
    assert steer.job_id == "JOB-1"
    assert steer.instruction == "UI는 건드리지 마"

    redirect = router.parse("/redirect --job JOB-1 DB 구조부터 다시 잡아")
    assert redirect.intent == Intent.REDIRECT
    assert redirect.job_id == "JOB-1"
    assert redirect.instruction == "DB 구조부터 다시 잡아"


def test_retry_sessions_and_doctor_commands(project_registry):
    router = CommandRouter(project_registry)

    retry = router.parse("/retry JOB-1")
    assert retry.intent == Intent.RETRY
    assert retry.job_id == "JOB-1"

    sessions = router.parse("/sessions")
    assert sessions.intent == Intent.SESSIONS

    current_session = router.parse("/session")
    assert current_session.intent == Intent.SESSION
    assert current_session.job_id is None

    new_session = router.parse("/session new")
    assert new_session.intent == Intent.NEW_SESSION

    use_session = router.parse("/session use SESSION-OLD")
    assert use_session.intent == Intent.USE_SESSION
    assert use_session.job_id == "SESSION-OLD"

    session = router.parse("/session SESSION-1")
    assert session.intent == Intent.SESSION
    assert session.job_id == "SESSION-1"

    doctor = router.parse("/doctor")
    assert doctor.intent == Intent.DOCTOR
