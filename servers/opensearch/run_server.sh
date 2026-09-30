#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
  python3 -m venv "$SCRIPT_DIR/.venv"
fi

"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" >/dev/null

# The version_notifier module lives at repo root under resources/. Match the
# runtime container layout by putting it on PYTHONPATH so ``from
# version_notifier import install`` works when running locally too.
export PYTHONPATH="$SCRIPT_DIR/../../resources:${PYTHONPATH:-}"

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py"
