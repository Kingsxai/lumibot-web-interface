#!/bin/bash
# Cron wrapper for symbol_research.py (2026-09-13) -- cheap check every
# 5 minutes, weekdays. Real (slow) LLM research only happens for symbols
# that actually rotated into the universe or went stale since the last
# check -- see symbol_research.py's own module docstring for the full
# design. A quiet cycle where nothing's new costs zero LLM calls and
# exits in well under a second.
#
# `claude` is only on the interactive shell's PATH (nvm-installed) -- same
# root cause and fix already found and documented in the lumibot-alpaca-ai
# project's run_daily_cycle.sh. Without this, every cron-triggered run
# fails silently at the subprocess.run(["claude", ...]) call inside
# symbol_research.py with a plain FileNotFoundError, caught and logged
# there but never actually researching anything.
export PATH="/home/VMbot01/.nvm/versions/node/v22.23.2/bin:$PATH"

set -uo pipefail

PROJECT_DIR="/home/VMbot01/lumibot-web-interface"
LOG_FILE="$PROJECT_DIR/symbol_research.log"

ny_dow="$(TZ='America/New_York' date +%u)"    # 1=Mon .. 7=Sun
if [ "$ny_dow" -gt 5 ]; then
    exit 0
fi

cd "$PROJECT_DIR"
echo "$(date -Iseconds) - starting check" >> "$LOG_FILE"
venv/bin/python symbol_research.py >> "$LOG_FILE" 2>&1
echo "$(date -Iseconds) - done" >> "$LOG_FILE"
