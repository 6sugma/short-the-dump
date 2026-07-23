$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\short-the-dump --db demo.db seed-demo
.\.venv\Scripts\short-the-dump --db demo.db serve --host 127.0.0.1 --port 8000
