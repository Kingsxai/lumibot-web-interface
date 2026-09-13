#!/bin/bash
# Self-healing watchdog for the lumibot-web-interface trading loop.
#
# Root cause (found via py-spy thread dump, 2026-08-31): Lumibot's own
# strategy_executor.py runs an internal APScheduler-based loop that
# intermittently stops re-firing the periodic on_trading_iteration job,
# while the Flask API thread and other background threads stay alive and
# responsive — so /api/status keeps reporting "trading" even though
# nothing is actually happening. Confirmed NOT fixed by upgrading Lumibot
# 4.5.86 -> 4.5.87 (strategy_executor.py/broker.py/trader.py are byte-
# identical between the two versions). Patching Lumibot's own source
# directly is out of scope (too risky on a live trading system) — this
# watchdog instead detects the symptom (stale heartbeat) via the
# last_iteration_* fields added to /api/status and self-heals by
# restarting the whole process, which reliably clears the hang.
#
# Runs every minute via cron. Idempotent and rate-limited: skips if the
# bot isn't running (nothing to watch), and won't restart more than once
# per COOLDOWN_SECONDS to avoid a restart storm if something else is
# wrong (e.g. broker auth failing right after every restart).

set -uo pipefail

PROJECT_DIR="/home/VMbot01/lumibot-web-interface"
LOG_FILE="$PROJECT_DIR/watchdog.log"
SESSION_NAME="lumibot"
STALL_THRESHOLD_SECONDS=180
# 2026-09-10: regime_router.py's cold-cache first refresh can take several
# minutes (up to ~50 scanner candidates x a real IB historical-data round
# trip each, ~10s/symbol observed live) -- 150s was sized before that
# existed and was causing a self-reinforcing restart loop (every restart
# forced a new cold refresh that also couldn't finish in time). Widened
# to give one real scan room to complete; the disk-persisted cache
# (regime_router.py) is the real fix for repeat restarts, this is just a
# backstop for the genuinely-first cold start.
STARTUP_GRACE_SECONDS=900
COOLDOWN_SECONDS=180
COOLDOWN_FILE="$PROJECT_DIR/.watchdog_last_restart"

# 2026-09-05: every real open position's stop-loss/take-profit/time-limit
# protection is enforced entirely in-process (_check_bracket_orders,
# strategy_manager.py's own on_trading_iteration loop) -- NOT as broker-
# native conditional orders (_place_bracket_order submits a plain entry
# order, confirmed by reading it). That loop only runs while is_trading
# is True, and bot_runner.py deliberately does NOT auto-resume trading
# after a restart (2026-09-02 fix for unwanted silent auto-resume of NEW
# entries) -- so before this flag existed, ANY watchdog-triggered restart
# left every open position with ZERO protection until a human noticed and
# clicked Start, which could be hours. This marker tells bot_runner.py
# "this specific restart was a self-heal, not a fresh/manual start" so it
# can auto-resume PROTECTION ONLY (is_trading=True, existing positions'
# reconcile_positions()-restored bracket tracking starts getting checked
# again within one iteration) while still forcing new_entries_paused=1 so
# no NEW trade is ever placed without a human's explicit un-pause --
# preserving the exact guarantee the 2026-09-02 fix was for.
SAFETY_RESUME_FLAG_FILE="$PROJECT_DIR/.watchdog_safety_resume"

# --- IB Gateway coverage (2026-09-03) — the local IB Gateway process
# (java via IBC, in its own "ib_gateway" tmux session, listening on
# port 4002) had ZERO monitoring before this: bot_runner.py can't
# function without it, but nothing detected either a full crash (the
# whole tmux session vanishing, Xvfb surviving — happened once
# overnight 2026-09-02/03) or a stuck reconnection after IBKR's own
# servers had a connectivity blip (happened a second time same night)
# until a human noticed /api/status failing and investigated by hand.
# User's explicit instruction: don't wait for a third time — detect
# and self-heal this the same way bot_runner.py's own stalls already
# are, with its own separate cooldown so the two checks never fight
# over one rate limiter.
IB_GATEWAY_SESSION_NAME="ib_gateway"
IB_GATEWAY_PORT=4002
IB_GATEWAY_START_SCRIPT="/home/VMbot01/ibkr/start_ib_gateway.sh"
IB_GATEWAY_STARTUP_TIMEOUT_SECONDS=120
IB_GATEWAY_COOLDOWN_SECONDS=180
IB_GATEWAY_COOLDOWN_FILE="$PROJECT_DIR/.watchdog_last_ib_gateway_restart"

# 2026-09-05: IBKR's security policy can require MANUAL 2FA (IBKR Mobile
# push) on first login / after a forced weekly logout, which IBC cannot
# bypass (see start_ib_gateway.sh) — every other failure this watchdog
# handles is self-healing, but this ONE genuinely needs a human's phone,
# and every restart attempt before this only logged a normal-looking line
# easy to miss in a minute-by-minute log. IB_GATEWAY_DOWN_SINCE_FILE
# records the FIRST failed attempt (untouched by later retries) so
# elapsed downtime can be measured across repeated 180s-cooldown cycles;
# cleared the moment the gateway is next confirmed up. Escalation fires
# once elapsed downtime crosses this threshold — well under the ~1h
# tolerance an open, now-unprotected position can safely sit for, so
# there's still real time to notice and approve 2FA.
IB_GATEWAY_DOWN_SINCE_FILE="$PROJECT_DIR/.watchdog_ib_gateway_down_since"
IB_GATEWAY_ESCALATION_SECONDS=1800

cd "$PROJECT_DIR"

log() {
    echo "$(date -Iseconds) - $1" >> "$LOG_FILE"
}

ib_gateway_port_up() {
    ss -tln 2>/dev/null | grep -q ":${IB_GATEWAY_PORT} "
}

# Self-heal the bot_runner.py process — same restart-and-wait sequence
# used below for its own stall detection, factored out so the IB
# Gateway recovery path (Gateway can't function without a reconnect on
# this side either) can reuse it without duplicating the logic.
restart_lumibot_session() {
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null
    sleep 2
    # 2026-09-08: found live -- tmux's default shell here is sh (dash),
    # where `source` doesn't exist. Every restart via this line has been
    # silently failing (the pane's shell errors on `source`, the whole
    # `&&` chain never reaches python, tmux drops the pane) while this
    # script kept logging "Restart complete" -- the actual root cause of
    # bot_runner.py being down since 2026-09-05 despite watchdog "fixing"
    # it repeatedly. Explicit /bin/bash -c avoids depending on tmux's
    # default shell at all; venv/bin/python directly avoids needing
    # `source`/activate in the first place.
    tmux new-session -d -s "$SESSION_NAME" -- \
        /bin/bash -c "cd '$PROJECT_DIR' && venv/bin/python bot_runner.py"
    for i in $(seq 1 15); do
        sleep 1
        if curl -s -m 3 -H "X-API-Key: $API_KEY" http://127.0.0.1:5000/api/status > /dev/null 2>&1; then
            break
        fi
    done
}

# API requires DASHBOARD_API_KEY as of 2026-08-31 (see api.py's
# require_api_key before_request hook) — read it straight from .env since
# this script isn't a Python process that loads it via dotenv/config.py.
API_KEY="$(grep '^DASHBOARD_API_KEY=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2-)"
ALPACA_API_KEY="$(grep '^ALPACA_API_KEY=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2-)"
ALPACA_API_SECRET="$(grep '^ALPACA_API_SECRET=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2-)"
ALPACA_IS_PAPER="$(grep '^ALPACA_IS_PAPER=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2-)"
if [ "${ALPACA_IS_PAPER:-true}" = "false" ]; then
    ALPACA_BASE_URL="https://api.alpaca.markets"
else
    ALPACA_BASE_URL="https://paper-api.alpaca.markets"
fi

# 2026-09-03: the stall-check below used to gate on market_is_open()
# (Alpaca's REGULAR-hours clock) because the main bot legitimately slept
# outside 09:30-16:00 ET — Lumibot's own default NASDAQ market calendar.
# That's no longer true: strategy_manager.py now calls set_market('24/5')
# so on_trading_iteration runs continuously Monday-Friday (extended-hours
# US, international LSE/ASX, and forex all trade through it too). Gating
# the stall-check on REGULAR hours now would recreate exactly the kind of
# blind spot this project already burned time on once (a genuinely dead
# loop during pre-market went undetected for hours on 2026-09-03 morning,
# though that specific case turned out to be legitimate pre-market
# dormancy under the OLD architecture — under the new one, a stall during
# those same hours is real and must be caught). Weekday-only now, no
# regular-vs-extended distinction.
is_trading_day() {
    local dow
    dow="$(date +%u)"  # 1=Monday ... 7=Sunday
    [ "$dow" -ge 1 ] && [ "$dow" -le 5 ]
}

# --- IB Gateway health check — runs every time, independent of whether
# the lumibot session exists (Gateway can crash while bot_runner.py is
# still technically alive-but-failing, or vice versa).
#
# 2026-09-10: gated on BROKER_PROVIDER (Alpaca-only move — see
# project memory, IB's per-share commission ate this account's thin
# scalping margins). bot_runner.py no longer touches IB Gateway on
# this broker at all, but this block previously ran unconditionally —
# an IB Gateway hiccup (crash, forced weekly 2FA logout) would still
# have restarted bot_runner.py "to reconnect" to a broker the live
# bot isn't even using anymore, needlessly disrupting real Alpaca
# trading. IB Gateway itself is left running (kept per explicit user
# direction, for the archived ib_legacy/ code "later") — only this
# watchdog's reaction to its health is skipped now.
broker_provider="$(grep -m1 '^BROKER_PROVIDER=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"

ib_gateway_session_exists=0
if tmux has-session -t "$IB_GATEWAY_SESSION_NAME" 2>/dev/null; then
    ib_gateway_session_exists=1
fi

if [ "$broker_provider" != "interactive_brokers" ]; then
    :  # Alpaca (or any non-IB broker) — skip IB Gateway health entirely.
elif [ "$ib_gateway_session_exists" -eq 0 ] || ! ib_gateway_port_up; then
    now_epoch="$(date +%s)"

    if [ ! -f "$IB_GATEWAY_DOWN_SINCE_FILE" ]; then
        echo "$now_epoch" > "$IB_GATEWAY_DOWN_SINCE_FILE"
    fi
    down_since="$(cat "$IB_GATEWAY_DOWN_SINCE_FILE" 2>/dev/null || echo "$now_epoch")"
    downtime_seconds=$((now_epoch - down_since))
    if [ "$downtime_seconds" -ge "$IB_GATEWAY_ESCALATION_SECONDS" ]; then
        log "ESCALATION: IB Gateway has been down for ${downtime_seconds}s (>${IB_GATEWAY_ESCALATION_SECONDS}s) — likely stuck waiting on manual 2FA (IBKR Mobile push). This cannot self-heal. Any real open position sits UNPROTECTED until this is resolved by hand."
    fi

    skip_gateway_restart=0
    if [ -f "$IB_GATEWAY_COOLDOWN_FILE" ]; then
        last_gw_restart="$(cat "$IB_GATEWAY_COOLDOWN_FILE" 2>/dev/null || echo 0)"
        if [ $((now_epoch - last_gw_restart)) -lt "$IB_GATEWAY_COOLDOWN_SECONDS" ]; then
            skip_gateway_restart=1
        fi
    fi

    if [ "$skip_gateway_restart" -eq 1 ]; then
        log "IB GATEWAY DOWN (session_exists=$ib_gateway_session_exists) but within cooldown, skipping restart"
    else
        log "IB GATEWAY DOWN: session_exists=$ib_gateway_session_exists — restarting"
        echo "$now_epoch" > "$IB_GATEWAY_COOLDOWN_FILE"

        # Xvfb (the virtual display IB Gateway renders into) is a
        # separate long-lived process this script never touches —
        # start_ib_gateway.sh itself detects and reuses it if already
        # running, only starting a fresh one if genuinely absent.
        tmux kill-session -t "$IB_GATEWAY_SESSION_NAME" 2>/dev/null
        sleep 2
        tmux new-session -d -s "$IB_GATEWAY_SESSION_NAME" "$IB_GATEWAY_START_SCRIPT"

        gateway_up=0
        waited=0
        while [ "$waited" -lt "$IB_GATEWAY_STARTUP_TIMEOUT_SECONDS" ]; do
            sleep 5
            waited=$((waited + 5))
            if ib_gateway_port_up; then
                gateway_up=1
                break
            fi
        done

        if [ "$gateway_up" -eq 1 ]; then
            log "IB Gateway restart complete (port ${IB_GATEWAY_PORT} up after ${waited}s) — restarting bot_runner.py to reconnect"
            rm -f "$IB_GATEWAY_DOWN_SINCE_FILE"
            date -Iseconds > "$SAFETY_RESUME_FLAG_FILE"
            restart_lumibot_session
            log "Restart complete — trading NOT auto-resumed, needs an explicit Start"
        else
            # IBKR's security policy can require MANUAL 2FA (IBKR Mobile
            # push) on first login / after IBKR's forced weekly logout —
            # IBC cannot bypass this (see start_ib_gateway.sh's own
            # header comment). Log distinctly rather than silently
            # retrying forever, so it's obvious from the log alone that
            # this specific failure needs a human's phone, not another
            # automated retry.
            log "IB GATEWAY RESTART FAILED — port ${IB_GATEWAY_PORT} did not come up within ${IB_GATEWAY_STARTUP_TIMEOUT_SECONDS}s, may need manual 2FA approval (IBKR Mobile push)"
        fi
    fi
else
    # Gateway healthy this run — clear any stale down-since marker so a
    # FUTURE outage measures its own downtime from scratch, not from a
    # leftover timestamp of a past, already-resolved outage.
    rm -f "$IB_GATEWAY_DOWN_SINCE_FILE"
fi

# Nothing to watch if the bot isn't running at all — scripts/start_bot_cron.sh
# owns bringing it up at market open; a manual stop by the user is not this
# watchdog's business to override.
if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    exit 0
fi

# 2026-09-02: was -m 5. Found live that a legitimate, still-alive iteration
# (many strategies x symbols, each a synchronous IB round trip) can leave
# the Flask thread too GIL-starved to answer within 5s even though the
# process finishes that same iteration within the STALL_THRESHOLD/
# STARTUP_GRACE budget below — two restarts (15:13:06, 15:16:06) fired on
# exactly this false-positive ("API unreachable") while iterations were
# genuinely completing in 13-70s. Widened so a busy-but-alive process has
# room to respond; a truly dead process still gets caught (tmux session
# check above + this timeout together), just not on a hair trigger.
status_json="$(curl -s -m 20 -H "X-API-Key: $API_KEY" http://127.0.0.1:5000/api/status 2>/dev/null)"

restart_needed=0
reason=""

if [ -z "$status_json" ]; then
    restart_needed=1
    reason="API unreachable while tmux session is alive"
else
    status="$(echo "$status_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)"
    seconds_since="$(echo "$status_json" | python3 -c "import json,sys; v=json.load(sys.stdin).get('seconds_since_last_iteration'); print(v if v is not None else '')" 2>/dev/null)"
    trading_started_at="$(echo "$status_json" | python3 -c "import json,sys; v=json.load(sys.stdin).get('trading_started_at'); print(v if v is not None else '')" 2>/dev/null)"

    if [ "$status" = "trading" ] && is_trading_day; then
        if [ -z "$seconds_since" ]; then
            # No iteration has completed yet — only a problem if enough
            # time has passed SINCE TRADING ACTUALLY STARTED (trading_
            # started_at, set by strategy_bot.start_trading() when
            # Start is clicked) that one should have run by now.
            #
            # 2026-09-03: was anchored to tmux session creation time
            # instead — broke the very first real end-to-end test after
            # today's set_market('24/5') consolidation. A bot idle
            # (is_trading=False) for a while has a session age already
            # past STARTUP_GRACE_SECONDS by the time Start finally gets
            # clicked; Lumibot's own scheduler runs on a fixed ~60s
            # cadence independent of when is_trading flips, so the very
            # next legitimate iteration can be up to ~60s away — but the
            # old check restarted the process only ~20s after Start,
            # long before that iteration was ever due. Falls back to
            # session-creation time only if trading_started_at is
            # missing (e.g. mid-deploy, an older bot_runner.py process).
            if [ -n "$trading_started_at" ]; then
                grace_reference_epoch="$(date -d "$trading_started_at" +%s 2>/dev/null)"
                grace_reference_label="trading started"
            fi
            if [ -z "$grace_reference_epoch" ]; then
                grace_reference_epoch="$(tmux list-sessions -F '#{session_name} #{session_created}' 2>/dev/null | awk -v s="$SESSION_NAME" '$1==s {print $2}')"
                grace_reference_label="session created"
            fi
            now_epoch="$(date +%s)"
            if [ -n "$grace_reference_epoch" ] && [ $((now_epoch - grace_reference_epoch)) -gt "$STARTUP_GRACE_SECONDS" ]; then
                restart_needed=1
                reason="status=trading but no iteration has ever completed, ${grace_reference_label} $((now_epoch - grace_reference_epoch))s ago"
            fi
        else
            seconds_int="${seconds_since%.*}"
            if [ "$seconds_int" -gt "$STALL_THRESHOLD_SECONDS" ]; then
                restart_needed=1
                reason="seconds_since_last_iteration=$seconds_since exceeds ${STALL_THRESHOLD_SECONDS}s threshold"
            fi
        fi
    fi
fi

if [ "$restart_needed" -eq 0 ]; then
    exit 0
fi

# Rate-limit restarts.
now_epoch="$(date +%s)"
if [ -f "$COOLDOWN_FILE" ]; then
    last_restart="$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)"
    if [ $((now_epoch - last_restart)) -lt "$COOLDOWN_SECONDS" ]; then
        log "STALL DETECTED ($reason) but within cooldown, skipping restart"
        exit 0
    fi
fi

log "STALL DETECTED: $reason — restarting"
echo "$now_epoch" > "$COOLDOWN_FILE"
date -Iseconds > "$SAFETY_RESUME_FLAG_FILE"

# 2026-09-02: deliberately does NOT call /api/start anymore — the user
# found the bot "restarting on its own" with no one clicking anything,
# traced to this exact auto-resume. With extended-hours/international-
# markets now in play too (which the process restart itself already
# resets to off, see bot_runner.py), an unattended process restart
# should never also auto-resume active trading — that always needs an
# explicit human/AI-directed command, same manual-gate principle as the
# dashboard Start button itself. Self-healing the process is still this
# watchdog's job; resuming trading is not.
restart_lumibot_session

log "Restart complete — trading NOT auto-resumed, needs an explicit Start"
