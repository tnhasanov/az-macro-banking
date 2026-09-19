#!/usr/bin/env bash
# Scheduled entry point: check official sources and generate the reports that are due.
#
# Intended for a daily scheduler (09:00 Asia/Baku by default). Idempotent: a run with no new
# publications only refreshes state; report editions are created only when the reporting policy
# in azmonitor/scheduling/run_due.py says one is due. Exit codes: 0 ok / no-op, 2 partial
# (some datasets failed, prior outputs preserved), 3 failed, 4 skipped because another run holds
# the lock.
#
# Environment:
#   AZMONITOR_DATA_DIR    persistent dataset directory (default ./data)
#   AZMONITOR_OUTPUT_DIR  report editions directory (default ./outputs)
#   AZMONITOR_RESTORE_CMD optional command run before the check (e.g. sync state from object storage)
#   AZMONITOR_SAVE_CMD    optional command run after the check (e.g. sync state back)
#   .env in the repository root is sourced if present (never commit it).
set -uo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export TZ="${TZ:-Asia/Baku}"
export AZMONITOR_DATA_DIR="${AZMONITOR_DATA_DIR:-$PWD/data}"
export AZMONITOR_OUTPUT_DIR="${AZMONITOR_OUTPUT_DIR:-$PWD/outputs}"
mkdir -p "$AZMONITOR_DATA_DIR/logs"
LOG="$AZMONITOR_DATA_DIR/logs/run_due_$(date +%Y%m%d).log"
echo "=== run_due start $(date -Is)" | tee -a "$LOG"

if [ -n "${AZMONITOR_RESTORE_CMD:-}" ]; then
  echo "restoring persistent state" | tee -a "$LOG"
  bash -c "$AZMONITOR_RESTORE_CMD" 2>&1 | tee -a "$LOG" || { echo "restore failed" | tee -a "$LOG"; exit 3; }
fi

PY="${PYTHON:-python3}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
"$PY" -m azmonitor.cli run-due "$@" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
STATUS=$("$PY" - <<'EOF'
import json, os, pathlib
p = pathlib.Path(os.environ["AZMONITOR_DATA_DIR"]) / "state" / "last_run_due.json"
print(json.load(open(p)).get("status", "unknown") if p.exists() else "unknown")
EOF
)
echo "run_due status: $STATUS (exit $RC)" | tee -a "$LOG"

if [ -n "${AZMONITOR_SAVE_CMD:-}" ]; then
  echo "saving persistent state" | tee -a "$LOG"
  bash -c "$AZMONITOR_SAVE_CMD" 2>&1 | tee -a "$LOG" || { echo "save failed" | tee -a "$LOG"; exit 3; }
fi
echo "=== run_due end $(date -Is)" | tee -a "$LOG"
case "$STATUS" in
  ok) exit 0;; partial) exit 2;; skipped_locked) exit 4;; failed) exit 3;; *) exit "$RC";;
esac
