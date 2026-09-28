#!/usr/bin/env bash
# Prepare a commentary session: refresh the sources, check the data, and write the request pack a
# Claude Code session drafts from. No model API key is used anywhere in this path.
#
# After this finishes, run the `/monthly-commentary` command in a Claude Code session in this
# repository, or follow .claude/commands/monthly-commentary.md by hand.
#
# Exit codes: 0 ready to draft, 2 data-quality problems that need attention first, 3 refresh failed.
set -uo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export TZ="${TZ:-Asia/Baku}"
export AZMONITOR_DATA_DIR="${AZMONITOR_DATA_DIR:-$PWD/data}"
export AZMONITOR_OUTPUT_DIR="${AZMONITOR_OUTPUT_DIR:-$PWD/outputs}"
PY="${PYTHON:-python3}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi

echo "== refreshing sources"
"$PY" -m azmonitor.cli refresh || { echo "refresh failed"; exit 3; }

echo "== data-quality checks"
"$PY" -m azmonitor.cli validate > /dev/null
CRITICAL=$("$PY" - <<'EOF'
import json, os, pathlib
p = pathlib.Path(os.environ["AZMONITOR_DATA_DIR"]) / "state" / "quality_report.json"
print(json.load(open(p))["summary"].get("critical", 0) if p.exists() else 0)
EOF
)
if [ "$CRITICAL" != "0" ]; then
  echo "$CRITICAL critical data-quality failure(s): publication is blocked until they are resolved"
  echo "see docs/quality_exceptions.md and data/state/quality_report.json"
  exit 2
fi

echo "== building the commentary request"
"$PY" -m azmonitor.cli commentary-pack

cat <<'MSG'

Next: draft the narrative.
  - in Claude Code:   /monthly-commentary
  - by hand:          follow .claude/commands/monthly-commentary.md
  - then render:      python -m azmonitor.cli report --type monthly --narrative-file narratives/<file>.json

Every number you write is checked against the fact pack before anything is rendered; whatever fails
falls back to facts-only text for that item and is recorded in the manifest.
MSG
