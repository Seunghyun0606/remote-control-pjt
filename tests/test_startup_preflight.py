from __future__ import annotations

from pathlib import Path

import pytest

from remote_control.cli import _run_controller
from remote_control.diagnostics import validate_controller_configuration
from remote_control.settings import Settings


def test_controller_configuration_preflight_accepts_valid_telegram():
    settings = Settings(
        _env_file=None,
        CONTROLLER_RUNNER_TOKEN="runner-secret",
        TELEGRAM_BOT_TOKEN="telegram-secret",
        TELEGRAM_ALLOWED_USER_IDS="100",
    )

    validate_controller_configuration(settings, no_telegram=False)


def test_controller_configuration_preflight_allows_explicit_no_telegram():
    settings = Settings(
        _env_file=None,
        CONTROLLER_RUNNER_TOKEN="runner-secret",
    )

    validate_controller_configuration(settings, no_telegram=True)


def test_controller_configuration_preflight_aggregates_static_failures():
    settings = Settings(
        _env_file=None,
        REMOTE_CONTROL_API_HOST="0.0.0.0",
        REMOTE_CONTROL_SLACK_ENABLED=True,
    )

    with pytest.raises(RuntimeError) as exc:
        validate_controller_configuration(settings, no_telegram=False)

    message = str(exc.value)
    assert "CONTROLLER_RUNNER_TOKEN is required" in message
    assert "CONTROLLER_API_TOKEN is required" in message
    assert "TELEGRAM_BOT_TOKEN is required" in message
    assert "TELEGRAM_ALLOWED_USER_IDS is required" in message
    assert "SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required" in message
    assert "SLACK_ALLOWED_USER_IDS is required" in message


def test_controller_configuration_preflight_rejects_invalid_telegram_allowlist():
    settings = Settings(
        _env_file=None,
        CONTROLLER_RUNNER_TOKEN="runner-secret",
        TELEGRAM_BOT_TOKEN="telegram-secret",
        TELEGRAM_ALLOWED_USER_IDS="100,not-an-integer",
    )

    with pytest.raises(RuntimeError, match="comma-separated list of integers"):
        validate_controller_configuration(settings, no_telegram=False)


@pytest.mark.asyncio
async def test_invalid_messaging_configuration_fails_before_database_creation(
    tmp_path: Path,
):
    home = tmp_path / "runtime"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"REMOTE_CONTROL_HOME={home.as_posix()}",
                "REMOTE_CONTROL_DB_URL=sqlite+aiosqlite:///./state/remote-control.db",
                "CONTROLLER_RUNNER_TOKEN=runner-secret",
                # Telegram is intentionally enabled by CLI but not configured.
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match="TELEGRAM_BOT_TOKEN is required",
    ):
        await _run_controller(no_telegram=False, env_file=env_file)

    assert not (home / "state" / "remote-control.db").exists()
    assert not (home / "config" / "projects.yaml").exists()
