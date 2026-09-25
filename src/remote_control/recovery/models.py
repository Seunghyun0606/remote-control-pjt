from __future__ import annotations

from enum import StrEnum


class RecoveryKind(StrEnum):
    HOST = "HOST"
    QUOTA = "QUOTA"
    RESTART = "RESTART"


class RecoveryMode(StrEnum):
    START = "START"
    RESUME = "RESUME"
    ADOPT = "ADOPT"
    FINALIZE = "FINALIZE"
    CANCEL = "CANCEL"
