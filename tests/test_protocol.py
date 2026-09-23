import pytest
from pydantic import ValidationError

from remote_control.transport.protocol import Envelope, message


def test_protocol_round_trip():
    encoded = message("HEARTBEAT", host_id="desktop-main").model_dump_json()
    decoded = Envelope.model_validate_json(encoded)
    assert decoded.protocol_version == 1
    assert decoded.type == "HEARTBEAT"
    assert decoded.payload["host_id"] == "desktop-main"


def test_protocol_rejects_unknown_version():
    with pytest.raises(ValidationError):
        Envelope.model_validate(
            {
                "protocol_version": 99,
                "type": "HEARTBEAT",
                "payload": {},
            }
        )
