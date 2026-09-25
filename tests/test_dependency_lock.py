from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _requirement_name(spec: str) -> str:
    name = re.split(r"\s*[<>=!~\[]", spec, maxsplit=1)[0]
    return _canonical_name(name.strip())


def _lock_names() -> set[str]:
    names: set[str] = set()
    for raw in (ROOT / "constraints" / "lock.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==", 1)[0].strip()
        names.add(_canonical_name(name))
    return names


def test_full_dependency_lock_covers_all_declared_direct_dependencies():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    declared = {
        _requirement_name(spec)
        for spec in (
            list(project["dependencies"])
            + list(project["optional-dependencies"]["dev"])
        )
    }

    locked = _lock_names()

    assert declared <= locked
    assert len(locked) > len(declared)


def test_full_dependency_lock_contains_exact_pins_only():
    for raw in (ROOT / "constraints" / "lock.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert line.count("==") == 1
        name, version = line.split("==", 1)
        assert name.strip()
        assert version.strip()
