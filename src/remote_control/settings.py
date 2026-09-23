from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
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

    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    telegram_allowed_user_ids_raw: str = Field(
        default="",
        validation_alias="TELEGRAM_ALLOWED_USER_IDS",
    )

    codex_executable: str = Field(default="codex", validation_alias="CODEX_EXECUTABLE")
    codex_sandbox: str = Field(default="workspace-write", validation_alias="CODEX_SANDBOX")
    codex_approval_policy: str = Field(
        default="never",
        validation_alias="CODEX_APPROVAL_POLICY",
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
