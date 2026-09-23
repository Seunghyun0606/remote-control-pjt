from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_control.projects.registry import ProjectRegistry
from remote_control.storage.db import Database


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def project_registry(tmp_path: Path, project_dir: Path) -> ProjectRegistry:
    config = {
        "projects": {
            "demo": {
                "name": "Demo Project",
                "adapter": "generic_git",
                "repository": {"path": {"lightsail-main": str(project_dir)}},
                "allowed_hosts": ["lightsail-main"],
                "default_host": "lightsail-main",
            }
        }
    }
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return ProjectRegistry.from_yaml(path)


@pytest.fixture
async def database(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.init()
    try:
        yield db
    finally:
        await db.close()
