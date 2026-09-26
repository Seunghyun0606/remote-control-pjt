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



def _hashed_requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--hash="):
            continue
        assert "==" in line
        requirement = line.rstrip("\\").strip()
        name, version = requirement.split("==", 1)
        assert name.strip()
        assert version.strip()
        assert index + 1 < len(lines)
        hash_line = lines[index + 1].strip()
        assert hash_line.startswith("--hash=sha256:")
        assert len(hash_line.removeprefix("--hash=sha256:")) == 64
        names.add(_canonical_name(name.strip()))
    return names


def test_build_backend_and_bootstrap_toolchain_are_exactly_locked():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["build-system"]["requires"] == ["hatchling==1.27.0"]

    build_names = _hashed_requirement_names(ROOT / "constraints" / "build.txt")
    assert build_names == {
        "hatchling",
        "editables",
        "packaging",
        "pathspec",
        "pluggy",
        "trove-classifiers",
    }

    pip_names = _hashed_requirement_names(ROOT / "constraints" / "pip.txt")
    assert pip_names == {"pip"}


def test_ci_disables_dynamic_build_isolation_and_uses_hash_locks():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "pip install --upgrade pip" not in workflow
    assert workflow.count("--require-hashes --only-binary=:all:") == 4
    assert workflow.count("--no-deps -r constraints/build.txt") == 2
    assert workflow.count("--no-build-isolation -c constraints/lock.txt") == 2
    assert workflow.count("run: pytest -q") == 2


def test_windows_ci_runs_full_suite_instead_of_a_curated_smoke_list():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    windows_section = workflow.split("windows-test:", 1)[1]
    assert "run: pytest -q" in windows_section
    assert "tests/test_r6_dashboard.py" not in windows_section
    assert "tests/test_final_hardening.py" not in windows_section



def test_windows_timezone_fallback_is_declared_and_locked():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = list(data["project"]["dependencies"])
    assert (
        "tzdata>=2026.4,<2027; sys_platform == 'win32'"
        in dependencies
    )

    lock = (ROOT / "constraints" / "lock.txt").read_text(encoding="utf-8")
    assert 'tzdata==2026.4; sys_platform == "win32"' in lock
