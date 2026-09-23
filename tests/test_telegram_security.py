from remote_control.messaging.telegram import is_authorized


def test_numeric_user_allowlist():
    assert is_authorized(100, {100, 200})
    assert not is_authorized(300, {100, 200})
