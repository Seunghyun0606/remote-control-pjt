from __future__ import annotations

import platform
from functools import lru_cache

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


class Settings(_BaseSettings):
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
    runner_token: str = Field(default="", validation_alias="CONTROLLER_RUNNER_TOKEN")

    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    telegram_allowed_user_ids_raw: str = Field(
        default="",
        validation_alias="TELEGRAM_ALLOWED_USER_IDS",
    )

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
        default="codex,git",
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
