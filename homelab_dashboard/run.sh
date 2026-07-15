#!/usr/bin/env bash
set -e
# Add-on-Optionen liegen unter /data/options.json (vom Supervisor bereitgestellt).
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --no-access-log
