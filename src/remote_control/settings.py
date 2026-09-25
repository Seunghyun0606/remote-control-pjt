from __future__ import annotations

import platform
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _BaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    codex_executable: str = Field(default="codex", validation_alias="CODEX_EXECUTABLE")
    codex_sandbox: str = Field(default="workspace-write", validation_alias="CODEX_SANDBOX")
    codex_approval_policy: str = Field(
        default="never",
        validation_alias="CODEX_APPROVAL_POLICY",
    )
    codex_home: str | None = Field(
        default=None,
        validation_alias="CODEX_HOME",
    )
    projectctl_executable: str = Field(
        default="projectctl",
        validation_alias="PROJECTCTL_EXECUTABLE",
    )
    git_executable: str = Field(default="git", validation_alias="GIT_EXECUTABLE")
    project_operation_timeout_seconds: int = Field(
        default=30,
        validation_alias="REMOTE_CONTROL_PROJECT_OPERATION_TIMEOUT_SECONDS",
    )


class Settings(_BaseSettings):
    home_path: str | None = Field(
        default=None,
        validation_alias="REMOTE_CONTROL_HOME",
    )
    db_url: str = Field(
        default="sqlite+aiosqlite:///./remote-control.db",
        validation_alias="REMOTE_CONTROL_DB_URL",
    )
    config_path: str = Field(
        default="./config/projects.yaml",
        validation_alias="REMOTE_CONTROL_CONFIG",
    )
    host_id: str = Field(default="lightsail-main", validation_alias="REMOTE_CONTROL_HOST_ID")
    api_host: str = Field(default="127.0.0.1", validation_alias="REMOTE_CONTROL_API_HOST")
    api_port: int = Field(default=8787, validation_alias="REMOTE_CONTROL_API_PORT")
    heartbeat_timeout_seconds: int = Field(
        default=45,
        validation_alias="REMOTE_CONTROL_HEARTBEAT_TIMEOUT_SECONDS",
    )
    progress_interval_seconds: int = Field(
        default=300,
        validation_alias="REMOTE_CONTROL_PROGRESS_INTERVAL_SECONDS",
    )
    scheduler_interval_seconds: int = Field(
        default=15,
        validation_alias="REMOTE_CONTROL_SCHEDULER_INTERVAL_SECONDS",
    )
    quota_retry_initial_seconds: int = Field(
        default=1800,
        validation_alias="REMOTE_CONTROL_QUOTA_RETRY_INITIAL_SECONDS",
    )
    quota_retry_max_seconds: int = Field(
        default=7200,
        validation_alias="REMOTE_CONTROL_QUOTA_RETRY_MAX_SECONDS",
    )
    quota_reset_grace_seconds: int = Field(
        default=600,
        validation_alias="REMOTE_CONTROL_QUOTA_RESET_GRACE_SECONDS",
    )
    restart_grace_seconds: int = Field(
        default=10,
        validation_alias="REMOTE_CONTROL_RESTART_GRACE_SECONDS",
    )
    runner_token: str = Field(default="", validation_alias="CONTROLLER_RUNNER_TOKEN")

    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    telegram_allowed_user_ids_raw: str = Field(
        default="",
        validation_alias="TELEGRAM_ALLOWED_USER_IDS",
    )
    slack_enabled: bool = Field(
        default=False,
        validation_alias="REMOTE_CONTROL_SLACK_ENABLED",
    )
    slack_bot_token: str | None = Field(
        default=None,
        validation_alias="SLACK_BOT_TOKEN",
    )
    slack_app_token: str | None = Field(
        default=None,
        validation_alias="SLACK_APP_TOKEN",
    )
    slack_allowed_user_ids_raw: str = Field(
        default="",
        validation_alias="SLACK_ALLOWED_USER_IDS",
    )
    web_ui_enabled: bool = Field(
        default=True,
        validation_alias="REMOTE_CONTROL_WEB_UI_ENABLED",
    )

    @property
    def resolved_home_path(self) -> Path:
        if self.home_path:
            return Path(self.home_path).expanduser().resolve()
        return Path.cwd().resolve()

    @property
    def resolved_config_path(self) -> Path:
        path = Path(self.config_path).expanduser()
        if path.is_absolute():
            return path
        return (self.resolved_home_path / path).resolve()

    @property
    def resolved_db_url(self) -> str:
        prefix = "sqlite+aiosqlite:///"
        if not self.db_url.startswith(prefix):
            return self.db_url
        raw_path = self.db_url[len(prefix) :]
        if raw_path == ":memory:":
            return self.db_url
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.resolved_home_path / path).resolve()
        return f"{prefix}{path.as_posix()}"

    @property
    def telegram_allowed_user_ids(self) -> set[int]:
        try:
            return {
                int(part.strip())
                for part in self.telegram_allowed_user_ids_raw.split(",")
                if part.strip()
            }
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers"
            ) from exc

    @property
    def slack_allowed_user_ids(self) -> set[str]:
        return {
            part.strip()
            for part in self.slack_allowed_user_ids_raw.split(",")
            if part.strip()
        }


class RunnerSettings(_BaseSettings):
    controller_ws: str = Field(
        default="ws://127.0.0.1:8787/ws/runner",
        validation_alias="REMOTE_RUNNER_CONTROLLER_WS",
    )
    token: str = Field(default="", validation_alias="REMOTE_RUNNER_TOKEN")
    host_id: str = Field(default="desktop-main", validation_alias="REMOTE_RUNNER_HOST_ID")
    name: str = Field(default="Desktop Runner", validation_alias="REMOTE_RUNNER_NAME")
    os_name: str = Field(
        default_factory=lambda: platform.system().lower(),
        validation_alias="REMOTE_RUNNER_OS",
    )
    capabilities_raw: str = Field(
        default="codex,git,projectctl",
        validation_alias="REMOTE_RUNNER_CAPABILITIES",
    )
    heartbeat_seconds: int = Field(
        default=15,
        validation_alias="REMOTE_RUNNER_HEARTBEAT_SECONDS",
    )
    reconnect_seconds: int = Field(
        default=5,
        validation_alias="REMOTE_RUNNER_RECONNECT_SECONDS",
    )

    @property
    def capabilities(self) -> set[str]:
        return {part.strip() for part in self.capabilities_raw.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
