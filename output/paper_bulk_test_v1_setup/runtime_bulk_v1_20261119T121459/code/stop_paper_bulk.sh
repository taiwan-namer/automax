#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_ROOT="$(cd "$ROOT/../data_root" && pwd)"
export BREAKOUT_RUNTIME_ROOT="$DATA_ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
exec python3 -u -m breakout_follow_v1.paper_main --paper --profile paper_bulk_test_v1 --runtime-root "$DATA_ROOT" --stop
