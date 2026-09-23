from remote_control.settings import Settings


def test_allowed_user_ids_parse_from_comma_string():
    settings = Settings(TELEGRAM_ALLOWED_USER_IDS="100, 200")
    assert settings.telegram_allowed_user_ids == {100, 200}
