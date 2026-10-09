#!/usr/bin/env bash
set -euo pipefail
echo "Starting GTSPP monthly processing: ${JOB_MODE:-staging}"
exec python /app/scripts/update_gtspp.py "$@"
