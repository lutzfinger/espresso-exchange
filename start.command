#!/bin/bash
# Double-click to run The Espresso Exchange locally on http://localhost:9300
cd "$(dirname "$0")"
[ -d .venv ] || (uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt)
HOST_KEY=${HOST_KEY:-local-host} DB_PATH=data/local.db .venv/bin/uvicorn server.app:app --port 9300 &
sleep 2 && open http://localhost:9300/screen
wait
