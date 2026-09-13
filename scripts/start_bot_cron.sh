#!/bin/bash
# Launches bot_runner.py 5 minutes before US market open (9:25 AM ET,
# Mon-Fri), inside a named tmux session so it can be attached to and
# watched live. Only starts the process — trading itself still requires
# an explicit Start click on the dashboard (POST /api/start). Does NOT
# account for market holidays; fires every weekday regardless.
#
# This system's cron daemon (3.0pl1, Ubuntu) does not support per-crontab
# timezones — TZ/CRON_TZ set in a crontab only affects the launched
# command's environment, not when cron decides to fire (confirmed via
# `man 5 crontab`). The daemon schedules purely in the system timezone
# (UTC on this VM), which would drift relative to ET across DST changes
# if this were scheduled directly. So instead: cron fires this script
# every minute, and the script itself checks the real America/New_York
# clock and only proceeds when it's exactly 09:25 on a weekday — the
# timezone-safe pattern documented in `man 5 crontab` itself.

set -euo pipefail

PROJECT_DIR="/home/VMbot01/lumibot-web-interface"
LOG_FILE="$PROJECT_DIR/cron_launch.log"
SESSION_NAME="lumibot"

ny_time="$(TZ='America/New_York' date +%H%M)"
ny_dow="$(TZ='America/New_York' date +%u)"  # 1=Mon .. 7=Sun

if [ "$ny_time" != "0925" ] || [ "$ny_dow" -gt 5 ]; then
    exit 0
fi

cd "$PROJECT_DIR"

if pgrep -f "python.*bot_runner\.py" > /dev/null; then
    echo "$(date -Iseconds) - bot_runner.py already running, skipping launch" >> "$LOG_FILE"
    exit 0
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "$(date -Iseconds) - tmux session '$SESSION_NAME' already exists, skipping launch" >> "$LOG_FILE"
    exit 0
fi

echo "$(date -Iseconds) - launching bot_runner.py in tmux session '$SESSION_NAME'" >> "$LOG_FILE"

# 2026-09-08: same fix as watchdog.sh's restart_lumibot_session -- tmux's
# default shell here is sh (dash), where `source` doesn't exist, so this
# line has been silently failing (this is the actual root cause of the
# bot being down since 2026-09-05, not anything watchdog-side). Explicit
# /bin/bash -c + venv/bin/python directly avoids both the default-shell
# dependency and the need for `source`/activate at all.
tmux new-session -d -s "$SESSION_NAME" -- \
    /bin/bash -c "cd '$PROJECT_DIR' && venv/bin/python bot_runner.py"

echo "$(date -Iseconds) - tmux session '$SESSION_NAME' started (attach with: tmux attach -t $SESSION_NAME)" >> "$LOG_FILE"
