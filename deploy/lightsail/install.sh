#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/remote-control-pjt}"
PYTHON="${PYTHON:-python3.11}"

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "Run this script after cloning the repository to $APP_DIR" >&2
  exit 1
fi

cd "$APP_DIR"
"$PYTHON" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created $APP_DIR/.env. Configure it before starting the service."
fi

echo "Install deploy/systemd/remote-control.service after reviewing User and paths."
