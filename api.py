"""Flask REST API and WebSocket server for trading bot control."""

import logging
import json
import os
import secrets
import threading
import time
import psutil
from datetime import datetime
from typing import Dict, Any

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, disconnect

import config
from lumibot.entities import Order
from lumibot.brokers import InteractiveBrokers
from strategy_manager import MultiStrategyBot
from signal_logger import (
    get_all_settings, set_setting, init_db, get_connection,
    get_all_strategy_toggles, set_strategy_enabled, ALL_STRATEGY_NAMES,
    get_all_strategy_risk, set_strategy_risk_override, clear_strategy_risk_override,
    get_performance_overview, get_strategy_performance, get_recent_closed_orders,
)

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
# Random per-process-start — only used to sign Flask's own session cookie,
# which this app doesn't rely on for anything security-sensitive (auth is
# the separate DASHBOARD_API_KEY check below). Previously a hardcoded
# guessable string; fixed 2026-08-31 alongside the API-key hardening.
app.config["SECRET_KEY"] = secrets.token_hex(32)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")


@app.before_request
def require_api_key():
    """Require DASHBOARD_API_KEY on every /api/* route (added 2026-08-31 —
    this API was found publicly reachable with zero authentication, able
    to place orders / start-stop trading / change risk settings). The `/`
    dashboard page itself stays open so the page shell loads; its JS then
    authenticates every actual API call and the socket.io connection.
    Auth is skipped entirely if DASHBOARD_API_KEY isn't set (local dev)."""
    if not config.DASHBOARD_API_KEY:
        return
    if not request.path.startswith("/api/"):
        return
    supplied = request.headers.get("X-API-Key") or request.args.get("key")
    if not supplied or not secrets.compare_digest(supplied, config.DASHBOARD_API_KEY):
        return jsonify({"error": "unauthorized"}), 401

# Initialize the signal logging database (creates tables/defaults if needed)
init_db()

# Global strategy instance
strategy_bot: MultiStrategyBot = None
# 2026-09-02: both point at the SAME ib_side_channel_trader.py instance
# now (one IB connection covers extended-hours + international markets)
# — kept as two names since api.py's routes/UI already distinguish the
# two toggles/stat cards; see init_extended_hours_trader/init_
# international_markets_trader below.
extended_hours_trader = None
international_markets_trader = None
connected_clients = set()
# Created once (not per-request) — psutil's cpu_percent() needs a prior
# call to compare against for a meaningful non-blocking reading; a fresh
# Process() object on every request would always report 0.0%.
_process_handle = psutil.Process(os.getpid())
_process_handle.cpu_percent(interval=None)  # prime it


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def format_position(position):
    """Format a position object for JSON response.

    2026-09-02: was crashing the whole endpoint (float(None)) for any
    position international_markets_trader.py placed — Lumibot's own
    get_positions() DOES see them (IB reports account-wide positions to
    every connected client), but since Lumibot didn't originate the
    order, its Position object has no fill-price context of its own
    (avg_fill_price is None). Falls back to our own tracked entry_price
    from international_markets_trader.open_positions when Lumibot's own
    data is missing, rather than a made-up 0.0 — that data is real, we
    just have to go get it from the right place.

    current_price/unrealized_pnl for these symbols also come from that
    same tracked dict (its "current_price", refreshed every
    LOOP_INTERVAL_SECONDS by that module's own _check_exits, and seeded
    at entry/reconciliation time so it's never blank) rather than
    Lumibot's get_last_price(), which was found to hang 30+ seconds (not
    just return None) on a non-US symbol — it has no data source
    configured for LSE/ASX names at all and apparently retries/blocks
    rather than failing fast. Calling it here made /api/positions (and
    therefore the dashboard's Open Positions table, the user's ONLY
    visibility into the account — see project_single_ib_login_constraint
    memory) effectively unusable the moment an international position
    existed. Never call it for these; use the cached value, possibly
    stale by up to LOOP_INTERVAL_SECONDS, but real and fast.

    2026-09-02: the original version gated this shortcut on
    `avg_fill_price is None` — true only briefly. IB reports account-wide
    fills to every connected client (see module docstrings), so Lumibot's
    own broker eventually backfills avg_fill_price for these positions
    too from that account-wide execution report, permanently and
    silently disabling the shortcut a short while after each position
    opens. Once that happened, get_last_price() was back on every
    broadcast_update() (fires every 2s per connected dashboard tab via
    the 'request_update' socket event) — confirmed live as the likely
    cause of a recurring self-inflicted restart loop: the hang blocks
    Flask's single-threaded dev server long enough for watchdog.sh's 5s
    /api/status check to see "API unreachable" and restart the whole
    process, which reconnects international_markets_trader and re-primes
    the same hang shortly after. Now keyed on tracked-symbol membership
    directly instead, which stays true for the position's whole life.
    """
    avg_fill_price = position.avg_fill_price
    is_international = False
    tracked = None
    if international_markets_trader:
        tracked = international_markets_trader.open_positions.get(position.symbol)
        if tracked:
            is_international = True
            if avg_fill_price is None:
                avg_fill_price = tracked["entry_price"]
    if avg_fill_price is None:
        # 2026-09-03: the fallback above only ever covered aux/
        # international positions — a PLAIN regular-hours position (the
        # majority of what the main bot trades) had no fallback at all,
        # relying solely on Lumibot's own position.avg_fill_price, which
        # is None immediately after a fill until its own async backfill
        # (from IB's account-wide execution report) catches up. Found
        # live during a Test All Strategies sweep: rapid buy/sell churn
        # across many symbols meant that backfill never got the chance
        # to land before the next fill reset the race, so avg_fill_price
        # showed as a permanent 0.0 (and unrealized_pnl as a nonsense
        # current_price*quantity) for every open position on the
        # dashboard. We already reliably track a real entry price
        # ourselves via bracket_orders (the reference signal price
        # initially, corrected to the real fill price by
        # on_filled_order) — use it here too, same as the aux path does.
        bracket = strategy_bot.bracket_orders.get(position.symbol)
        if bracket and bracket.entry_price is not None:
            avg_fill_price = bracket.entry_price
    avg_fill_price = float(avg_fill_price) if avg_fill_price is not None else 0.0

    if is_international:
        last_price = tracked.get("current_price")
    else:
        last_price = strategy_bot.get_last_price(position.symbol)
    return {
        "symbol": position.symbol,
        "quantity": float(position.quantity),
        "avg_fill_price": avg_fill_price,
        "current_price": float(last_price or 0),
        "unrealized_pnl": float((last_price - avg_fill_price) * position.quantity) if last_price else 0.0,
    }


def format_order(order):
    """Format an order object for JSON response.

    This Lumibot version's Order object doesn't expose `.price`,
    `.created_at`, or `.filled_quantity` (confirmed 2026-08-31, live —
    they don't exist on this version's class at all, not just unset) —
    using the real equivalents instead: `.avg_fill_price`, and summing
    `.transactions` for filled quantity. `.created_at`'s only backing
    field (`_date_created`) has no public accessor in this version, so
    it's just omitted rather than reaching into a private attribute.
    """
    transactions = getattr(order, "transactions", None) or []
    filled_quantity = sum(float(t.quantity) for t in transactions) if transactions else 0.0
    avg_fill_price = getattr(order, "avg_fill_price", None)
    return {
        "id": str(getattr(order, "identifier", "")),
        "symbol": order.asset.symbol if order.asset else "UNKNOWN",
        "side": order.side,
        "quantity": float(order.quantity),
        "price": float(avg_fill_price) if avg_fill_price else None,
        "status": order.status,
        "filled_quantity": filled_quantity,
        "created_at": None,  # no public accessor in this Lumibot version, see docstring above
        "source": "main_bot",
    }


def is_any_trading_active() -> bool:
    """True if the main bot is actually able to place real orders right
    now. Used to be True whenever EITHER the main bot's is_trading flag
    OR the extended-hours/international-markets dashboard toggle was on,
    because before the 2026-09-03 consolidation those ran as independent
    background threads that never checked is_trading at all (found live
    2026-09-02: real paper trades kept happening while the badge said
    STOPPED). That's no longer true — extended-hours/international/forex
    now route through self.ib_aux from inside on_trading_iteration
    itself, gated by the SAME is_trading flag as everything else (see
    strategy_manager.py's on_trading_iteration: `if not self.is_trading:
    return` happens before any of it runs). The toggles now only control
    which entries are ALLOWED once trading is active, not whether
    trading is active — so this is just is_trading again. (Found live
    2026-09-03: flipping both toggles with Start never clicked made this
    report "trading" while the main loop was genuinely idle, feeding
    watchdog.sh a false stall signal mid-test.)"""
    return strategy_bot.is_trading


_last_broadcast_at = 0.0
BROADCAST_MIN_INTERVAL_SECONDS = 5.0  # matches the dashboard's own REST poll cadence


def broadcast_update():
    """Broadcast bot status update to all connected clients.

    2026-09-02: field names now match /api/status exactly
    (buying_power, positions_count, open_orders_count,
    bracket_orders_count) — they didn't before (this sent bare
    open_orders/bracket_orders as counts under different names, and
    never sent buying_power at all), so the dashboard JS's `if
    (data.buying_power !== undefined)`-style guards silently skipped
    every one of those fields on every socket update after the initial
    page-load REST fetch — those cards were only ever fresh once, at
    load, then quietly went stale. The Recent Orders table's dead-code
    issue (open_orders sent as a count, not an array, so
    Array.isArray() always failed) is fixed separately by switching the
    dashboard to poll /api/orders directly instead.

    2026-09-02 (second fix, same day): this makes ~6 separate live IB
    calls (get_portfolio_value/get_cash/get_buying_power/get_positions
    x2 — redundantly, once for the list and again just for its own
    length/get_orders), and the dashboard's own 'request_update' socket
    ping fires every 2s per open tab — confirmed live: "The queue was
    empty or max time reached for positions" was repeating every ~2s,
    exactly matching that interval, while the trading iteration was
    ALSO making its own sequential IB calls on the same clientId=1
    connection. That contention was intermittently starving /api/status
    too, tripping watchdog's "API unreachable" check and causing real
    restarts during live trading. Throttled to the same 5s cadence the
    REST polls already use — nothing is lost (the dashboard already has
    5s-fresh data from those), just stops firing MORE OFTEN than that on
    top of them. Also de-duped the double get_positions() call below.
    """
    global _last_broadcast_at
    if not strategy_bot or not connected_clients:
        return
    now = time.time()
    if now - _last_broadcast_at < BROADCAST_MIN_INTERVAL_SECONDS:
        return
    _last_broadcast_at = now

    try:
        positions = strategy_bot.get_positions()
        data = {
            "timestamp": datetime.now().isoformat(),
            "status": "trading" if is_any_trading_active() else "stopped",
            "portfolio_value": float(strategy_bot.get_portfolio_value()),
            "cash": float(strategy_bot.get_cash()),
            "buying_power": strategy_bot.get_buying_power(),
            # See /api/status's own comment -- this account's real base
            # currency is GBP, confirmed live against IB directly.
            "account_currency": "GBP" if isinstance(strategy_bot.broker, InteractiveBrokers) else "USD",
            "positions": [format_position(p) for p in positions],
            "positions_count": len(positions),
            "open_orders_count": len(strategy_bot.get_orders(statuses=Order.ACTIVE_STATUSES)),
            "bracket_orders_count": len(strategy_bot.bracket_orders),
        }
        socketio.emit("bot_update", data)
    except Exception as e:
        logger.error(f"Error broadcasting update: {e}")


# ============================================================================
# REST API ENDPOINTS
# ============================================================================

@app.route("/api/system-health", methods=["GET"])
def get_system_health():
    """Real CPU/memory/disk stats for the bot_runner.py process AND the
    host — not the fake "System Information" card the uploaded dashboard
    design had (Bot Version/Refresh Count with no real values). psutil
    is already a dependency (Lumibot itself uses it for the
    LUMIBOT_TELEMETRY log lines in bot.log), just never exposed via the
    API before."""
    try:
        mem = _process_handle.memory_info()
        vmem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return jsonify({
            "process": {
                "cpu_percent": _process_handle.cpu_percent(interval=None),
                "memory_mb": mem.rss / (1024 * 1024),
                "threads": _process_handle.num_threads(),
                "uptime_seconds": datetime.now().timestamp() - _process_handle.create_time(),
            },
            "system": {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "cpu_count": psutil.cpu_count(),
                "memory_percent": vmem.percent,
                "memory_used_gb": vmem.used / (1024 ** 3),
                "memory_total_gb": vmem.total / (1024 ** 3),
                "disk_percent": disk.percent,
                "disk_used_gb": disk.used / (1024 ** 3),
                "disk_total_gb": disk.total / (1024 ** 3),
            },
        })
    except Exception as e:
        logger.error(f"Error getting system health: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def get_status():
    """Get current bot status and portfolio state."""
    try:
        # Buying power isn't exposed on the Strategy class directly in this
        # Lumibot version — strategy_bot.get_buying_power() handles both
        # Alpaca and Interactive Brokers, falling back to cash (and then
        # 0.0) if every lookup fails, e.g. during a transient broker
        # outage/rate-limit (observed 2026-08-31).
        buying_power = strategy_bot.get_buying_power()

        started_at = strategy_bot.last_iteration_started_at
        completed_at = strategy_bot.last_iteration_completed_at
        seconds_since_last_iteration = (
            (datetime.now() - completed_at).total_seconds() if completed_at else None
        )
        trading_started_at = getattr(strategy_bot, "trading_started_at", None)

        # 2026-09-09: this account's real base currency is GBP, confirmed
        # live via a direct reqAccountSummary query — every top-line IB
        # figure (NetLiquidation, BuyingPower, AvailableFunds,
        # TotalCashValue) reports in GBP, not USD, despite this project's
        # dashboard/config having assumed "$"/USD everywhere until now
        # (see project_ib_currency_and_scanner_findings_2026_09_09
        # memory for the full investigation). Alpaca (Group 1, a
        # different project) is genuinely USD, unaffected by this.
        account_currency = "GBP" if isinstance(strategy_bot.broker, InteractiveBrokers) else "USD"

        return jsonify({
            "status": "trading" if is_any_trading_active() else "stopped",
            "portfolio_value": float(strategy_bot.get_portfolio_value()),
            "cash": float(strategy_bot.get_cash()),
            "buying_power": buying_power,
            "account_currency": account_currency,
            "positions_count": len(strategy_bot.get_positions()),
            "open_orders_count": len(strategy_bot.get_orders(statuses=Order.ACTIVE_STATUSES)),
            "bracket_orders_count": len(strategy_bot.bracket_orders),
            "last_iteration_started_at": started_at.isoformat() if started_at else None,
            "last_iteration_completed_at": completed_at.isoformat() if completed_at else None,
            "seconds_since_last_iteration": seconds_since_last_iteration,
            "trading_started_at": trading_started_at.isoformat() if trading_started_at else None,
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def get_positions():
    """Get all open positions."""
    try:
        positions = [format_position(p) for p in strategy_bot.get_positions()]
        return jsonify({"positions": positions})
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions/<symbol>/liquidate", methods=["POST"])
def liquidate_position(symbol):
    """Close ONE specific open position on demand (2026-09-03) —
    independent of Pause New Entries / Stop Trading. Built specifically
    so the new deploy workflow can close only the currently-profitable
    positions one at a time while a mixed green/red book is paused,
    without touching the red ones (see feedback_deploy_workflow_pause_
    liquidate_green memory).

    Two separate position-tracking systems exist and both need to be
    reachable here: strategy_bot (main bot, regular-hours trades) and
    the international_markets_trader/extended_hours_trader side channel
    (same object since the 2026-09-02 unification) for extended-hours/
    international trades, tracked in its own open_positions dict rather
    than strategy_bot.bracket_orders.

    Checked side-channel FIRST now (2026-09-03, was main-bot-first) —
    strategy_bot.get_position() queries the broker's raw IB position
    list for the whole account, which returns a hit for ANY open
    contract regardless of which side actually opened it. A real
    international position (AUTO/LSE) was matched by that main-bot
    check first, routed through strategy_bot._close_position(), which
    reported success but the order never reached IB at all (confirmed
    via direct reqAllOpenOrders() — zero open orders, position
    unchanged) — the main bot's generic order path isn't built with
    the right exchange/currency contract metadata for these symbols.
    side_channel.open_positions is authoritative for anything it
    actually opened (including positions reconciled on startup), so
    check it first and only fall back to the main bot for symbols it
    genuinely doesn't know about.
    """
    try:
        side_channel = international_markets_trader or extended_hours_trader
        if side_channel and symbol in side_channel.open_positions:
            side_channel._close_position(symbol, reason="manual_liquidate")
            return jsonify({"status": "liquidate attempted", "symbol": symbol, "source": "side_channel"})

        if strategy_bot.get_position(symbol):
            closed = strategy_bot._close_position(symbol, reason="manual_liquidate")
            if closed:
                return jsonify({"status": "liquidate attempted", "symbol": symbol, "source": "main_bot"})
            return jsonify({"error": f"Could not liquidate {symbol} — see server log (locked, deferred, or already closing)"}), 409

        return jsonify({"error": f"No open position found for {symbol}"}), 404
    except Exception as e:
        logger.error(f"Error liquidating position {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders", methods=["GET"])
def get_orders():
    """Get all orders (open and closed).

    2026-09-02: merges in ib_side_channel_trader.py's own order history
    (extended-hours + international markets, one merged module as of
    2026-09-02 — see its module docstring) — those orders go through a
    second raw ibapi client, invisible to strategy_bot.get_orders()
    (Lumibot's own broker-tracked orders), which is why the dashboard
    showed nothing for that activity before this fix. Sorted newest-
    first; side-channel orders carry a real created_at, main-bot orders
    don't (no accessor in this Lumibot version) so they sort after any
    side-channel orders in the same response — still returned, just not
    date-ordered relative to them.

    De-duped by id against the side-channel history: IB Gateway
    broadcasts open-order updates for the WHOLE account to every
    connected API client (confirmed live 2026-09-02 — "Download open
    orders on connection" is on), so Lumibot's own client (client ID 1)
    also picks up orders ib_side_channel_trader placed on its own
    connection (client ID 2), under the SAME order id, but with none of
    the real data (price/status/created_at all null/"unknown") since
    Lumibot's Order wrapper has no context for an order it didn't
    originate. Without de-duping, every side-channel order appeared
    twice — once correct, once garbage.

    extended_hours_trader and international_markets_trader are the SAME
    object as of 2026-09-02 (see init_extended_hours_trader/init_
    international_markets_trader below) — only read one of them here,
    reading both would double every entry.
    """
    try:
        side_channel_ids = set()
        orders = []
        if extended_hours_trader:
            orders.extend(extended_hours_trader.order_history)
            side_channel_ids.update(o["id"] for o in extended_hours_trader.order_history)
        orders.extend(
            format_order(o) for o in strategy_bot.get_orders()
            if str(getattr(o, "identifier", "")) not in side_channel_ids
        )
        orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        return jsonify({"orders": orders})
    except Exception as e:
        logger.error(f"Error getting orders: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/recent-activity", methods=["GET"])
def get_recent_activity():
    """Recent strategy activity: every logged BUY/SELL signal (from the
    main bot plus extended_hours_trader.py / international_markets_trader.py
    — all three log through the same signal_logger.log_signal, see
    strategy_manager.py's ALL_STRATEGY_NAMES module docstring), newest
    first, with which strategy fired it and a human-readable reason
    (for scalping's news-sentiment-rider entries, the sentiment score).

    `confirmed` reflects the secondary-indicator confirmation gate only —
    it is NOT proof an order was actually placed/filled (same caveat as
    confidence-stats' "fired" count, see project memory); cross-check
    against /api/orders for real execution when that matters.
    """
    try:
        limit = min(int(request.args.get("limit", 30)), 200)
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT timestamp, strategy_name, symbol, action, features, "
                "confirmed, entry_price FROM signals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        activity = []
        for row in rows:
            try:
                features = json.loads(row["features"]) if row["features"] else {}
            except (TypeError, ValueError):
                features = {}
            reason = features.get("reason") or f"{row['strategy_name']} {row['action']} signal"
            activity.append({
                "timestamp": row["timestamp"],
                "strategy": row["strategy_name"],
                "symbol": row["symbol"],
                "action": row["action"],
                "confirmed": bool(row["confirmed"]) if row["confirmed"] is not None else None,
                "entry_price": row["entry_price"],
                "reason": reason,
            })
        return jsonify({"activity": activity})
    except Exception as e:
        logger.error(f"Error getting recent activity: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance-overview", methods=["GET"])
def performance_overview():
    """Today's total REALIZED P&L (not open-position unrealized P&L) —
    2026-09-03, backed by signal_logger's new closed_trades ledger (real
    fill prices, both main-bot and side-channel trades). "Today" is the
    bot's own UTC calendar day, matching every other timestamp already
    used across this dashboard — deliberately not converted to ET."""
    try:
        return jsonify(get_performance_overview())
    except Exception as e:
        logger.error(f"Error getting performance overview: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategy-performance", methods=["GET"])
def strategy_performance():
    """Per-strategy realized P&L + win rate (2026-09-03). Only strategies
    with at least one closed trade are included — a strategy with zero
    closed trades yet is simply absent from the list rather than shown
    as a fake $0.00/0% row (dashboard renders that as an empty cell,
    same "Coming soon, not fake $0" convention used elsewhere here)."""
    try:
        return jsonify({"strategies": get_strategy_performance()})
    except Exception as e:
        logger.error(f"Error getting strategy performance: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/recent-closed-orders", methods=["GET"])
def recent_closed_orders():
    """Recently closed trades, newest first (2026-09-03) — real entry/exit
    fill prices and realized P&L for each, whether closed by a strategy's
    own exit signal, Stop Trading, or the per-position Liquidate button.
    Dashboard colors each row green/red by whether pnl was positive."""
    try:
        limit = min(int(request.args.get("limit", 20)), 200)
        return jsonify({"orders": get_recent_closed_orders(limit)})
    except Exception as e:
        logger.error(f"Error getting recent closed orders: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/news-alerts", methods=["GET"])
def get_news_alerts():
    """Real news/sentiment-driven alerts, newest first: severe-negative-
    news emergency liquidations (strategy_bot.get_news_emergency_stops,
    see strategy_manager._check_news_emergency_stop — critical severity)
    plus scalping's news-sentiment-rider entries (signals.db rows whose
    features->reason names the sentiment spike that sourced them, see
    scalping.py's get_signal_reason — info severity). Two already-real
    sources, not a new logging mechanism — a raw "sentiment crossed the
    relevance threshold" alert with no resulting trade isn't logged
    anywhere yet, so it can't appear here (see project_news_alerts_feed
    memory for the scoping reasoning).
    """
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
        alerts = []

        for stop in strategy_bot.get_news_emergency_stops():
            alerts.append({
                "timestamp": stop["timestamp"],
                "severity": "critical",
                "symbol": stop["symbol"],
                "message": f"News emergency stop — {stop['symbol']} liquidated, sentiment {stop['sentiment_score']:.2f}",
            })

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT timestamp, symbol, features FROM signals "
                "WHERE strategy_name = 'scalping' AND action = 'BUY' "
                "AND features LIKE '%News sentiment spike%' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                features = json.loads(row["features"]) if row["features"] else {}
            except (TypeError, ValueError):
                features = {}
            alerts.append({
                "timestamp": row["timestamp"],
                "severity": "info",
                "symbol": row["symbol"],
                "message": features.get("reason", "News sentiment spike"),
            })

        alerts.sort(key=lambda a: a["timestamp"] or "", reverse=True)
        return jsonify({"alerts": alerts[:limit]})
    except Exception as e:
        logger.error(f"Error getting news alerts: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders", methods=["POST"])
def place_order():
    """Place a new order."""
    try:
        data = request.get_json()
        symbol = data.get("symbol")
        quantity = float(data.get("quantity", 1))
        side = data.get("side", "buy")

        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400

        # Create and submit order
        order = strategy_bot.create_order(symbol, int(quantity), side)
        strategy_bot.submit_order(order)

        return jsonify({
            "order_id": str(getattr(order, "identifier", "")),
            "status": "submitted",
            "symbol": symbol,
            "quantity": quantity,
            "side": side,
        }), 201

    except Exception as e:
        logger.error(f"Error placing order: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
def start_bot():
    """Start the trading bot."""
    try:
        strategy_bot.start_trading()
        return jsonify({"status": "started"})
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    """Stop the trading bot and liquidate positions.

    2026-09-02: also disables extended-hours and international-markets
    trading — found live that Stop only ever touched the main Lumibot
    loop, leaving both background threads (which don't check
    strategy_bot.is_trading at all, only their own dashboard toggle)
    placing real orders regardless. A "Stop" that doesn't stop
    everything is a real safety gap, not just a cosmetic one — fixed so
    it's a genuine stop-all.

    2026-09-02 (second fix, same day): user reported positions still
    showing open after clicking Stop "a couple of times." Root cause:
    the old code only set a `shutdown_requested` flag — actual
    liquidation happened on the NEXT Lumibot on_trading_iteration, which
    never fired again once the market was closed (Lumibot's own default
    scheduler gates it on regular-hours market-open). Now liquidation
    runs immediately, in a background thread, regardless of market hours
    — doubly true since the 2026-09-03 consolidation's set_market('24/5')
    means on_trading_iteration itself runs continuously anyway.

    2026-09-03 consolidation: strategy_bot._liquidate_all_positions now
    routes EVERY position correctly on its own — regular-hours, extended-
    hours, international, and forex alike (previously extended-hours
    positions weren't covered by any path here at all, and international
    needed a separate excluded/threaded call to international_markets_
    trader._close_position to avoid double-closing). One call now
    handles all of it; no more manual exclusion/threading split needed.
    """
    try:
        strategy_bot.is_trading = False
        strategy_bot.shutdown_requested = True  # fallback: also liquidates on the next iteration

        threading.Thread(
            target=strategy_bot._liquidate_all_positions,
            daemon=True,
        ).start()

        return jsonify({"status": "stopping", "message": "Liquidating positions and disabling all trading (extended-hours, international-markets)..."})
    except Exception as e:
        logger.error(f"Error stopping bot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategies", methods=["GET"])
def get_strategies():
    """Get strategy performance metrics."""
    try:
        metrics = strategy_bot.get_performance_metrics()
        return jsonify(metrics)
    except Exception as e:
        logger.error(f"Error getting strategies: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/bracket-orders", methods=["GET"])
def get_bracket_orders():
    """Get all bracket orders."""
    try:
        bracket_data = []
        for symbol, bracket in strategy_bot.bracket_orders.items():
            bracket_data.append({
                "symbol": bracket.symbol,
                "quantity": bracket.quantity,
                "entry_price": float(bracket.entry_price),
                "stop_loss_price": float(bracket.stop_loss_price),
                "take_profit_price": float(bracket.take_profit_price),
                "current_price": float(getattr(bracket, 'current_price', bracket.entry_price)),
                "entry_time": bracket.entry_time.isoformat(),
            })
        return jsonify({"bracket_orders": bracket_data})
    except Exception as e:
        logger.error(f"Error getting bracket orders: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/signal-settings", methods=["GET"])
def get_signal_settings():
    """Get the current triple-barrier / meta-model confidence settings,
    plus (2026-09-12) the two dashboard-editable position-sizing figures
    (max_position_size, hard_position_ceiling_gbp) -- same generic
    signal_settings table/mechanism, just a different theme of setting."""
    try:
        return jsonify(get_all_settings())
    except Exception as e:
        logger.error(f"Error getting signal settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/signal-settings", methods=["POST"])
def update_signal_settings():
    """Update triple-barrier / meta-model confidence settings, and/or the
    position-sizing figures (max_position_size, hard_position_ceiling_gbp),
    from the dashboard. See project_max_position_size_raised_36pct_2026_09_12
    memory for why max_position_size is currently 0.37 and
    hard_position_ceiling_gbp is currently 250.0 -- not arbitrary numbers."""
    try:
        data = request.get_json()
        allowed_keys = {"stop_loss_percent", "take_profit_percent",
                         "time_limit_days", "min_signal_confidence",
                         "max_position_size", "hard_position_ceiling_gbp"}
        updated = {}
        for key, value in data.items():
            if key in allowed_keys:
                set_setting(key, float(value))
                updated[key] = float(value)
        return jsonify({"status": "updated", "settings": updated})
    except Exception as e:
        logger.error(f"Error updating signal settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pause-new-entries", methods=["GET"])
def get_pause_new_entries():
    """Get whether new-position entries are currently paused (see
    signal_logger.py's "new_entries_paused" default for the full
    reasoning — distinct from Stop, exits stay fully active)."""
    try:
        paused = get_all_settings().get("new_entries_paused", 0.0) >= 0.5
        return jsonify({"paused": paused})
    except Exception as e:
        logger.error(f"Error getting pause-new-entries state: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pause-new-entries", methods=["POST"])
def update_pause_new_entries():
    """Pause/resume new-position entries. Body: {"paused": true/false}.
    Blocks every entry call site (main bot, extended-hours, international
    equities, forex) while every exit mechanism stays fully active."""
    try:
        data = request.get_json()
        paused = bool(data.get("paused"))
        set_setting("new_entries_paused", 1.0 if paused else 0.0)
        return jsonify({"status": "updated", "paused": paused})
    except Exception as e:
        logger.error(f"Error updating pause-new-entries state: {e}")
        return jsonify({"error": str(e)}), 500


# --- Test All Strategies (2026-09-02, user-requested dashboard feature)
# ---------------------------------------------------------------------
# Places a real (paper) buy for one symbol from each strategy's own live
# universe, waits for it to fill, then closes it — one strategy at a
# time — to verify the order-placement path actually works end-to-end.
# Bypasses analyze()/confirm/confidence entirely, same as any manual
# order, so it may ONLY run while new entries are paused (enforced
# server-side, not just a disabled dashboard button) — it must never run
# concurrently with live strategy-driven entries. Runs in a background
# thread since a full sweep (8 strategies x fill-wait x close-wait) can
# take several minutes; the dashboard polls /status for live progress.
TEST_SWEEP_CAPITAL = 1000.0
TEST_SWEEP_FILL_TIMEOUT_SECONDS = 20
TEST_SWEEP_CLOSE_TIMEOUT_SECONDS = 90
TEST_SWEEP_POLL_INTERVAL_SECONDS = 3

_test_sweep_lock = threading.Lock()
_test_sweep_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "results": [],
}


def _get_test_symbol(strategy_name: str):
    """First symbol from that strategy's own live (already KID-restricted-
    filtered, see symbol_universe.py) universe — momentum is the one
    exception, its universe lives in config.MOMENTUM_UNIVERSE rather than
    a self.symbols attribute."""
    if strategy_name == "momentum":
        return config.MOMENTUM_UNIVERSE[0] if config.MOMENTUM_UNIVERSE else None
    strategy_obj = getattr(strategy_bot, strategy_name, None)
    symbols = getattr(strategy_obj, "symbols", None) if strategy_obj else None
    return symbols[0] if symbols else None


def _run_test_sweep():
    try:
        for name in ALL_STRATEGY_NAMES:
            entry = {"strategy": name, "symbol": None, "buy": "pending", "sell": "pending"}
            with _test_sweep_lock:
                _test_sweep_state["results"].append(entry)

            symbol = _get_test_symbol(name)
            if not symbol:
                entry["buy"] = "no symbol available in this strategy's universe"
                entry["sell"] = "skipped"
                continue
            entry["symbol"] = symbol

            try:
                strategy_bot._place_bracket_order(
                    symbol, "buy", TEST_SWEEP_CAPITAL, strategy_name=name, bypass_pause_gate=True
                )
            except Exception as e:
                entry["buy"] = f"error: {e}"
                entry["sell"] = "skipped"
                continue

            filled = False
            waited = 0
            while waited < TEST_SWEEP_FILL_TIMEOUT_SECONDS:
                time.sleep(TEST_SWEEP_POLL_INTERVAL_SECONDS)
                waited += TEST_SWEEP_POLL_INTERVAL_SECONDS
                if strategy_bot.get_position(symbol):
                    filled = True
                    break
            if not filled:
                entry["buy"] = "no fill (insufficient capital for 1 whole share, or broker-rejected)"
                entry["sell"] = "skipped"
                continue
            entry["buy"] = "filled"

            # A same-cycle wash-trade guard (_close_position's own
            # cycle_reservations check) can defer this close until the
            # next real trading iteration clears it — keep retrying
            # rather than giving up on the first attempt.
            #
            # CRITICAL: _close_position sells position.quantity — IB can
            # take longer than one poll interval to reflect a fill in
            # get_position(), so calling _close_position again on every
            # tick (regardless of whether the last call already
            # submitted a real order) resubmits a fresh sell for
            # whatever quantity is still showing, stacking on top of an
            # order still in flight. Confirmed live 2026-09-02: this
            # drove a 4-share NVDA test position to a -6785-share short
            # before it was caught and flattened. Fix: only call
            # _close_position while it keeps returning False (deferred/
            # blocked, meaning nothing was submitted) — the moment it
            # returns True, STOP calling it and just poll get_position()
            # for the order to actually settle.
            closed = False
            order_submitted = False
            waited = 0
            while waited < TEST_SWEEP_CLOSE_TIMEOUT_SECONDS:
                if not order_submitted:
                    try:
                        order_submitted = strategy_bot._close_position(symbol, strategy_name=name)
                    except Exception as e:
                        entry["sell"] = f"error: {e}"
                        break
                time.sleep(TEST_SWEEP_POLL_INTERVAL_SECONDS)
                waited += TEST_SWEEP_POLL_INTERVAL_SECONDS
                if not strategy_bot.get_position(symbol):
                    closed = True
                    break
            if entry["sell"] == "pending":
                entry["sell"] = "closed" if closed else "still open (deferred/timed out — check positions manually)"
    except Exception as e:
        logger.error(f"Error in test-all-strategies sweep: {e}", exc_info=True)
    finally:
        with _test_sweep_lock:
            _test_sweep_state["running"] = False
            _test_sweep_state["finished_at"] = datetime.now().isoformat()


@app.route("/api/test-all-strategies", methods=["POST"])
def start_test_all_strategies():
    """Kick off the test sweep (see module comment above). Requires new
    entries to already be paused — trading stays paused for the whole
    sweep and afterward, until an explicit Start, same manual-gate
    principle as everywhere else on this dashboard."""
    try:
        if get_all_settings().get("new_entries_paused", 0.0) < 0.5:
            return jsonify({"error": "New entries must be paused before running the strategy test"}), 400
        with _test_sweep_lock:
            if _test_sweep_state["running"]:
                return jsonify({"error": "A test sweep is already running"}), 409
            _test_sweep_state["running"] = True
            _test_sweep_state["started_at"] = datetime.now().isoformat()
            _test_sweep_state["finished_at"] = None
            _test_sweep_state["results"] = []
        threading.Thread(target=_run_test_sweep, daemon=True).start()
        return jsonify({"status": "started"})
    except Exception as e:
        logger.error(f"Error starting test-all-strategies: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test-all-strategies/status", methods=["GET"])
def get_test_all_strategies_status():
    try:
        with _test_sweep_lock:
            return jsonify(dict(_test_sweep_state))
    except Exception as e:
        logger.error(f"Error getting test-all-strategies status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/extended-hours-trading", methods=["GET"])
def get_extended_hours_trading():
    """Read-only stats for the extended-hours side of ib_aux (2026-09-03:
    the enable/disable toggle was removed — always on whenever the bot
    itself is trading, see ib_side_channel_trader.py's module
    docstring). Kept as a GET-only endpoint since the dashboard still
    shows these stats."""
    try:
        stats = extended_hours_trader.stats.get("extended_hours", {}) if extended_hours_trader else {}
        return jsonify({"stats": stats})
    except Exception as e:
        logger.error(f"Error getting extended-hours trading stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/international-markets-trading", methods=["GET"])
def get_international_markets_trading():
    """Read-only stats for the international/forex side of ib_aux
    (2026-09-03: the enable/disable toggle was removed — always on
    whenever the bot itself is trading, see ib_side_channel_trader.py's
    module docstring). Kept as a GET-only endpoint since the dashboard
    still shows these stats."""
    try:
        stats = international_markets_trader.stats.get("international", {}) if international_markets_trader else {}
        return jsonify({"stats": stats})
    except Exception as e:
        logger.error(f"Error getting international-markets trading stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/capital-anomalies", methods=["GET"])
def get_capital_anomalies():
    """Real record of every time IB reported a buying_power/
    portfolio_value implausible for this account's real size (2026-09-09,
    see config.CAPITAL_SANITY_THRESHOLD_GBP and
    project_100k_tsla_incident_and_hard_ceiling_fix memory — a real
    $100,271 TSLA fill on 2026-09-08 went undetected for a full day
    before this existed). Empty is the expected, healthy state; any
    entry here means the sanity check actually caught something and
    needs a human look, even though position sizing itself is already
    bounded by config.HARD_POSITION_CEILING_GBP regardless."""
    try:
        return jsonify({"anomalies": strategy_bot.capital_anomalies})
    except Exception as e:
        logger.error(f"Error getting capital anomalies: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tradeable-universe", methods=["GET"])
def get_tradeable_universe():
    """The "tradeable universe" screener (2026-09-09, explicit user
    request) — per international market, whether its currency is
    actually reachable right now (config.ACCOUNT_ESTABLISHED_CURRENCIES
    -- see that constant's own comment for the real $2,000-minimum-
    balance finding behind this) and the real candidates the scanner
    most recently found there, even for markets not yet tradeable (so
    the user can see what's WAITING to become reachable once real
    capital crosses that floor, not just what's live today). Read-only,
    reflects ib_side_channel_trader.py's own in-memory state — persists
    across dashboard page loads/logins as long as the bot process stays
    up, same as every other stats endpoint here."""
    try:
        universe = international_markets_trader.tradeable_universe if international_markets_trader else {}
        tradeable = {k: v for k, v in universe.items() if v.get("tradeable")}
        waiting = {k: v for k, v in universe.items() if not v.get("tradeable")}
        return jsonify({
            "established_currencies": sorted(config.ACCOUNT_ESTABLISHED_CURRENCIES),
            "tradeable_markets": tradeable,
            "waiting_on_currency": waiting,
        })
    except Exception as e:
        logger.error(f"Error getting tradeable universe: {e}")
        return jsonify({"error": str(e)}), 500


def _update_env_file(updates: dict):
    """Update or append KEY=VALUE lines in .env, preserving everything
    else. Used only for live-trading credentials (see
    /api/live-credentials below) — same plaintext-.env pattern this
    project already uses for Alpaca/IB-paper credentials, kept
    consistent rather than inventing a different storage mechanism for
    just this one case. Genuinely more sensitive than the paper
    credentials already there (this is the on-ramp to real money), but
    encrypting only this one file's entries while leaving the rest
    plaintext would be a false sense of security, not a real fix — if
    stronger protection is wanted, it needs to apply to the whole .env,
    a separate decision.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0]
            if key in remaining:
                lines[i] = f"{key}={remaining.pop(key)}\n"

    for key, value in remaining.items():
        lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)


@app.route("/api/account-mode", methods=["GET"])
def get_account_mode():
    """Get the dashboard's selected account mode (paper/live) — display
    and gating only, see signal_logger.py's account_mode_live comment
    for what this does and, importantly, does NOT do."""
    try:
        is_live = get_all_settings().get("account_mode_live", 0.0) >= 0.5
        return jsonify({"mode": "live" if is_live else "paper"})
    except Exception as e:
        logger.error(f"Error getting account mode: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/account-mode", methods=["POST"])
def update_account_mode():
    """Set the dashboard's selected account mode. Body: {"mode": "paper"|"live"}.
    Purely a stored preference — does not itself change where orders
    route (see signal_logger.py's account_mode_live comment); real live
    trading execution is not implemented."""
    try:
        data = request.get_json()
        mode = data.get("mode")
        if mode not in ("paper", "live"):
            return jsonify({"error": "mode must be 'paper' or 'live'"}), 400
        set_setting("account_mode_live", 1.0 if mode == "live" else 0.0)
        return jsonify({"status": "updated", "mode": mode})
    except Exception as e:
        logger.error(f"Error updating account mode: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live-credentials", methods=["GET"])
def get_live_credentials_status():
    """Whether live IBKR credentials are configured — returns a boolean
    only, NEVER the credentials themselves, even to an authenticated
    dashboard request."""
    try:
        configured = bool(os.environ.get("IB_LIVE_USERNAME")) and bool(os.environ.get("IB_LIVE_PASSWORD"))
        return jsonify({"configured": configured})
    except Exception as e:
        logger.error(f"Error checking live credentials status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live-credentials", methods=["POST"])
def set_live_credentials():
    """Store live IBKR credentials for future use. Body:
    {"username": ..., "password": ...}. This ONLY stores them in .env
    (IB_LIVE_USERNAME/IB_LIVE_PASSWORD) — it does not connect to a live
    IB Gateway, does not start live trading, and no code path currently
    reads these to route real orders. That's a separate, much larger
    task (a live IB Gateway instance, live order routing throughout
    strategy_manager.py and both background traders) — see
    project_lumibot_roadmap memory, Phase 5/6, not started without an
    explicit separate go-ahead."""
    try:
        data = request.get_json()
        username = (data or {}).get("username", "").strip()
        password = (data or {}).get("password", "")
        if not username or not password:
            return jsonify({"error": "username and password are required"}), 400
        _update_env_file({"IB_LIVE_USERNAME": username, "IB_LIVE_PASSWORD": password})
        os.environ["IB_LIVE_USERNAME"] = username
        os.environ["IB_LIVE_PASSWORD"] = password
        logger.info("Live IBKR credentials stored (not connected — live execution not implemented)")
        return jsonify({"status": "saved"})
    except Exception as e:
        logger.error(f"Error saving live credentials: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/confidence-stats", methods=["GET"])
def get_confidence_stats():
    """Get Phase 4 meta-model confidence gate stats: signals fired vs
    filtered per strategy, since the bot last started (in-memory only)."""
    try:
        return jsonify(strategy_bot.get_confidence_stats())
    except Exception as e:
        logger.error(f"Error getting confidence stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/regime-stats", methods=["GET"])
def get_regime_stats():
    """Get per-strategy regime-precedence stats: times a strategy lost a
    same-cycle, same-symbol contest to a better-regime-matched competitor,
    since the bot last started (in-memory only). See
    strategy_manager._resolve_regime_precedence / regime_matcher.STRATEGY_REGIMES."""
    try:
        return jsonify(strategy_bot.get_regime_precedence_stats())
    except Exception as e:
        logger.error(f"Error getting regime stats: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/news-emergency-stops", methods=["GET"])
def get_news_emergency_stops():
    """Get every liquidation triggered by the news emergency stop (any
    strategy's open position, closed on severe negative news), since the
    bot last started (in-memory only). See
    strategy_manager._check_news_emergency_stop."""
    try:
        return jsonify({"stops": strategy_bot.get_news_emergency_stops()})
    except Exception as e:
        logger.error(f"Error getting news emergency stops: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategy-preview", methods=["GET"])
def get_strategy_preview():
    """Diagnostic-only: re-run every strategy's analyze() right now,
    read-only, no orders/logging/gating. Used to check whether the live
    bot's signals.db is missing anything it should have caught."""
    try:
        return jsonify(strategy_bot.preview_all_decisions())
    except Exception as e:
        logger.error(f"Error getting strategy preview: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategy-toggles", methods=["GET"])
def get_strategy_toggles():
    """Get which strategies are currently enabled/disabled."""
    try:
        return jsonify(get_all_strategy_toggles())
    except Exception as e:
        logger.error(f"Error getting strategy toggles: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategy-toggles", methods=["POST"])
def update_strategy_toggles():
    """Enable or disable one or more strategies from the dashboard."""
    try:
        data = request.get_json()
        updated = {}
        for name, enabled in data.items():
            if name in ALL_STRATEGY_NAMES:
                set_strategy_enabled(name, bool(enabled))
                updated[name] = bool(enabled)
        return jsonify({"status": "updated", "toggles": updated})
    except Exception as e:
        logger.error(f"Error updating strategy toggles: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategy-risk", methods=["GET"])
def get_strategy_risk_settings():
    """Get each strategy's effective stop-loss/take-profit (its own
    override if set, otherwise the global default) and whether it's custom."""
    try:
        return jsonify(get_all_strategy_risk())
    except Exception as e:
        logger.error(f"Error getting strategy risk settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategy-risk", methods=["POST"])
def update_strategy_risk_settings():
    """Set or clear a strategy's custom stop-loss/take-profit from the
    dashboard. Body: {strategy_name: {custom: bool, stop_loss_percent,
    take_profit_percent}}. custom=false clears the override (reverts to
    the global default); custom=true requires both percent fields."""
    try:
        data = request.get_json()
        updated = {}
        for name, settings in data.items():
            if name not in ALL_STRATEGY_NAMES:
                continue
            if settings.get("custom"):
                set_strategy_risk_override(
                    name,
                    float(settings["stop_loss_percent"]),
                    float(settings["take_profit_percent"]),
                )
            else:
                clear_strategy_risk_override(name)
            updated[name] = settings
        return jsonify({"status": "updated", "risk_settings": updated})
    except Exception as e:
        logger.error(f"Error updating strategy risk settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    """Serve the web dashboard."""
    return render_template("dashboard.html")


# ============================================================================
# WEBSOCKET EVENTS
# ============================================================================

@socketio.on("connect")
def handle_connect():
    """Handle client connection. Requires DASHBOARD_API_KEY (passed as the
    socket.io connection query param) so anonymous connections can't
    listen in on live portfolio/status broadcasts — added 2026-08-31
    alongside the REST API key requirement."""
    if config.DASHBOARD_API_KEY:
        supplied = request.args.get("key")
        if not supplied or not secrets.compare_digest(supplied, config.DASHBOARD_API_KEY):
            logger.warning(f"Rejected unauthenticated socket connection from {request.remote_addr}")
            disconnect()
            return

    client_id = request.sid
    connected_clients.add(client_id)
    logger.info(f"Client connected: {client_id}")
    emit("connect_response", {"data": "Connected to trading bot"})


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    client_id = request.sid
    connected_clients.discard(client_id)
    logger.info(f"Client disconnected: {client_id}")


@socketio.on("request_update")
def handle_update_request():
    """Handle client request for status update."""
    broadcast_update()


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


def init_bot(bot_instance: MultiStrategyBot):
    """Initialize the API with a strategy bot instance."""
    global strategy_bot
    strategy_bot = bot_instance
    logger.info("API initialized with strategy bot")


def init_extended_hours_trader(trader_instance):
    """Initialize the API with the ib_side_channel_trader.py background
    loop instance (2026-09-02, was a separate extended_hours_trader.py
    module until the "one channel" merge), so
    /api/extended-hours-trading can report its stats."""
    global extended_hours_trader
    extended_hours_trader = trader_instance
    logger.info("API initialized with extended-hours trader")


def init_international_markets_trader(trader_instance):
    """Initialize the API with the ib_side_channel_trader.py background
    loop instance (2026-09-02, same object init_extended_hours_trader
    receives — see that function's docstring), so
    /api/international-markets-trading can report its stats."""
    global international_markets_trader
    international_markets_trader = trader_instance
    logger.info("API initialized with international markets trader")
