from __future__ import annotations

from typer.testing import CliRunner

from remote_control import __version__
from remote_control.cli import app


def test_root_version_option_reports_package_version():
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
