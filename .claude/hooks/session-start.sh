#!/bin/bash
# SessionStart hook for Claude Code on the web: install the project so tests, linters and the
# CLI work in a fresh container. Runs only in remote sessions; idempotent.
set -euo pipefail
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi
cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"
bash scripts/setup_env.sh --quiet
echo 'export TZ="Asia/Baku"' >> "${CLAUDE_ENV_FILE:-/dev/null}"
echo 'export PYTHONPATH="."' >> "${CLAUDE_ENV_FILE:-/dev/null}"
