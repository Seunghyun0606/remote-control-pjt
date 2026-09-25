from remote_control.settings import Settings


def test_allowed_user_ids_parse_from_comma_string():
    settings = Settings(TELEGRAM_ALLOWED_USER_IDS="100, 200")
    assert settings.telegram_allowed_user_ids == {100, 200}


def test_runtime_home_stabilizes_relative_paths(tmp_path):
    settings = Settings(
        _env_file=None,
        REMOTE_CONTROL_HOME=str(tmp_path),
        REMOTE_CONTROL_DB_URL="sqlite+aiosqlite:///./state/remote-control.db",
        REMOTE_CONTROL_CONFIG="./config/projects.yaml",
    )

    assert settings.resolved_home_path == tmp_path.resolve()
    assert settings.resolved_config_path == (tmp_path / "config" / "projects.yaml").resolve()
    assert settings.resolved_db_url == (
        "sqlite+aiosqlite:///"
        + (tmp_path / "state" / "remote-control.db").resolve().as_posix()
    )


def test_codex_home_is_loaded_for_child_runtime():
    settings = Settings(_env_file=None, CODEX_HOME="C:/Users/test/.codex")
    assert settings.codex_home == "C:/Users/test/.codex"


def test_quota_reset_grace_defaults_to_ten_minutes():
    settings = Settings(_env_file=None)
    assert settings.quota_reset_grace_seconds == 600


def test_quota_reset_grace_can_be_overridden():
    settings = Settings(
        _env_file=None,
        REMOTE_CONTROL_QUOTA_RESET_GRACE_SECONDS=900,
    )
    assert settings.quota_reset_grace_seconds == 900



def test_control_api_token_and_loopback_detection():
    local = Settings(_env_file=None, CONTROLLER_API_TOKEN="secret")
    assert local.api_token == "secret"
    assert local.api_is_loopback is True

    exposed = Settings(
        _env_file=None,
        REMOTE_CONTROL_API_HOST="0.0.0.0",
        CONTROLLER_API_TOKEN="secret",
    )
    assert exposed.api_is_loopback is False



def test_telegram_selection_ttl_defaults_and_override():
    default = Settings(_env_file=None)
    assert default.telegram_selection_ttl_seconds == 300

    overridden = Settings(
        _env_file=None,
        REMOTE_CONTROL_TELEGRAM_SELECTION_TTL_SECONDS=120,
    )
    assert overridden.telegram_selection_ttl_seconds == 120
