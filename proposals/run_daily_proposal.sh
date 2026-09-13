#!/bin/bash
# Group 2's daily proposal cycle (2026-09-07) -- 09:40 ET weekdays,
# offset 5 minutes from Group 1's 09:35 cycle so they don't compete for
# CPU/network at the same minute. Never executes anything -- the agent's
# tool scope (proposals/.claude_settings.json) has no Bash access at all,
# so it structurally cannot place an order or run a script that could,
# even if it wanted to. It only writes proposals/YYYY-MM-DD.md for a
# human to review.

set -uo pipefail

# 2026-09-08: found live (same bug independently found in Group 1's two
# cron scripts) -- `claude` is on the interactive shell's PATH only
# (nvm), not cron's minimal environment. Every cron-triggered run of
# this script has been failing at the `claude -p` call with "command
# not found" (exit 127) -- confirmed directly in this script's own log
# (daily_proposal.log, 2026-09-07 entry). Update the version below if
# nvm ever upgrades node (check with `which claude` interactively).
export PATH="/home/VMbot01/.nvm/versions/node/v22.23.2/bin:$PATH"

PROJECT_DIR="/home/VMbot01/lumibot-web-interface"
LOG_FILE="$PROJECT_DIR/proposals/daily_proposal.log"

ny_time="$(TZ='America/New_York' date +%H%M)"
ny_dow="$(TZ='America/New_York' date +%u)"

if [ "$ny_time" != "0940" ] || [ "$ny_dow" -gt 5 ]; then
    exit 0
fi

mkdir -p "$PROJECT_DIR/proposals"
log() { echo "$(date -Iseconds) - $1" >> "$LOG_FILE"; }

cd "$PROJECT_DIR"

log "Gathering today's context"
if ! venv/bin/python proposals/generate_context.py >> "$LOG_FILE" 2>&1; then
    log "generate_context.py failed, aborting"
    exit 1
fi

log "Invoking daily proposal agent"
claude -p --output-format json \
    --add-dir "$PROJECT_DIR" \
    --settings "$PROJECT_DIR/proposals/.claude_settings.json" \
    --dangerously-skip-permissions \
    "$(cat "$PROJECT_DIR/proposals/daily_proposal_prompt.md")" >> "$LOG_FILE" 2>&1

log "Proposal cycle complete -- see proposals/$(date +%Y-%m-%d).md"
