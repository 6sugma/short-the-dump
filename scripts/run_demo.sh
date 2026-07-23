#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/short-the-dump --db demo.db seed-demo
exec .venv/bin/short-the-dump --db demo.db serve --host 127.0.0.1 --port 8000
