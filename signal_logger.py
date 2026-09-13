"""SQLite-backed signal logging, configurable triple-barrier settings, and
per-strategy enable/disable toggles.

Every strategy signal (BUY/SELL) gets logged here with its indicator context
and confirmation result, so we can later label outcomes and train a
meta-model on which signals were actually reliable.
"""

import sqlite3
import json
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DB_PATH = "signals.db"

ALL_STRATEGY_NAMES = [
    "momentum", "scalping", "scalping_v2", "breakout",
    "mean_reversion", "vwap", "gap_and_go", "reversal", "market_profile",
    # 2026-09-04 five-category-pod cutover. pod_stock routes through
    # strategy_manager.py's existing _place_bracket_order (plain US-
    # domestic, USD, SMART-routed — the one shape it already handles).
    # pod_etf/pod_commodity/pod_forex/pod_crypto route through self.
    # ib_aux._place_entry directly with a real per-instrument contract
    # (currency/exchange/secType aware) — see _run_pod_strategies's
    # docstring for the full routing. All five seeded OFF below (same
    # reasoning: genuinely new, never-live-tested code, user enables each
    # deliberately once they've watched it work).
    "pod_stock", "pod_etf", "pod_commodity", "pod_forex", "pod_crypto",
    # 2026-09-11: 14 new strategies from this session's sizing/collision
    # marathon (see extended_strategies_live.py's module docstring for the
    # full derivation). Seeded OFF below alongside the pods -- genuinely
    # new, never-live-tested code.
    "lab_range", "supply_demand", "supertrend_200ema", "fakeout_breakout_fib",
    "macd_200ema", "ema_ribbon_smi", "discount_zone", "breakout_chandelier",
    "volume_divergence_grab", "asymmetric_dual", "rigorous_rr",
    "volume_absorption", "candle_taxonomy", "volume_profile",
]

# 2026-09-11: the 14 new strategy names above, seeded OFF the same way
# pod_*/scalping_v2 already are -- kept as its own set (rather than only
# the startswith("pod_") check) so the default_enabled logic below stays
# a single readable membership test instead of a long name enumeration
# inline.
_SEEDED_OFF_EXTRA = {
    "lab_range", "supply_demand", "supertrend_200ema", "fakeout_breakout_fib",
    "macd_200ema", "ema_ribbon_smi", "discount_zone", "breakout_chandelier",
    "volume_divergence_grab", "asymmetric_dual", "rigorous_rr",
    "volume_absorption", "candle_taxonomy", "volume_profile",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                features TEXT NOT NULL,
                confirmed INTEGER,
                outcome_label INTEGER,
                outcome_computed_at TEXT,
                exit_reason TEXT,
                exit_price REAL,
                entry_price REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_strategy_symbol
            ON signals(strategy_name, symbol, timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_unlabeled
            ON signals(outcome_label)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_settings (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL
            )
        """)
        defaults = {
            # Matches config.py's STOP_LOSS_PERCENT/TAKE_PROFIT_PERCENT — the
            # values that actually execute in _place_bracket_order. These
            # used to default to 0.02/0.03, disconnected from what really
            # executes (2026-08-30 fix — see project memory). One-time
            # migration below corrects any database still holding the old
            # stale defaults.
            "stop_loss_percent": 0.05,
            "take_profit_percent": 0.10,
            "time_limit_days": 5.0,
            "min_signal_confidence": 0.6,
            # 2026-09-01: dashboard-toggleable master switch for
            # 2026-09-03: extended_hours_trading_enabled/international_
            # markets_trading_enabled removed — no longer separate opt-in
            # toggles, always on whenever the bot itself is trading (see
            # ib_side_channel_trader.py's module docstring).
            # 2026-09-02: dashboard account-mode preference (0.0=paper,
            # 1.0=live) — display/preference only. Does NOT route any
            # trading differently on its own; no code path currently
            # reads this to change which broker/account real orders go
            # to. Building that is a separate, larger task (see
            # project_lumibot_roadmap memory, Phase 5/6) — this exists so
            # the dashboard can show which mode is *selected* and gate
            # the live-credentials UI, nothing more.
            "account_mode_live": 0.0,
            # 2026-09-02: "pause new entries" — user-requested transition
            # control, distinct from Stop (which force-liquidates
            # everything immediately). Blocks every entry/BUY call site
            # (main bot regular hours, extended hours, international
            # equities, forex) while leaving every EXIT mechanism fully
            # active (bracket stop/target, strategy SELL signals, POC
            # rejection, news emergency stop, market-close buffer) —
            # existing positions close via their own natural, designed
            # exit logic instead of being force-closed. Used to safely
            # deploy code changes once the book reaches flat, without
            # disrupting positions already in flight. Forced back to 0.0
            # on every bot_runner.py startup, same manual-gate pattern as
            # extended_hours/international toggles above — never starts a
            # fresh process silently paused.
            "new_entries_paused": 0.0,
            # 2026-09-12: dashboard-editable position-sizing figures.
            # Matches config.py's MAX_POSITION_SIZE/HARD_POSITION_CEILING_GBP
            # defaults exactly — those constants remain the FALLBACK
            # (get_setting's own `default` arg) if this row is ever missing,
            # same pattern as stop_loss_percent/take_profit_percent above.
            # See project_max_position_size_raised_36pct_2026_09_12 memory
            # for the full reasoning behind these two specific numbers.
            "max_position_size": 0.37,
            "hard_position_ceiling_gbp": 250.0,
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO signal_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # One-time migration: a database created before 2026-08-30 has these
        # pinned at the old, disconnected-from-execution 0.02/0.03 values.
        conn.execute(
            "UPDATE signal_settings SET value = 0.05 WHERE key = 'stop_loss_percent' AND value = 0.02"
        )
        conn.execute(
            "UPDATE signal_settings SET value = 0.10 WHERE key = 'take_profit_percent' AND value = 0.03"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_toggles (
                strategy_name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        for name in ALL_STRATEGY_NAMES:
            # 2026-09-04: pod_* strategies default OFF (0), not ON like the
            # 8 original strategies — genuinely new, never-live-tested
            # code; the user turns each on deliberately once they've
            # watched it work. Only affects the FIRST time a name's row is
            # created (INSERT OR IGNORE never touches an existing row), so
            # this doesn't reset anyone's dashboard toggle choice on a
            # normal restart. scalping_v2 (2026-09-08) gets the same
            # treatment for the same reason — genuinely new, never-live-
            # tested entry logic (MACD/Stoch RSI confluence), the user
            # enables it deliberately once they've watched it work.
            default_enabled = 0 if (name.startswith("pod_") or name == "scalping_v2" or name in _SEEDED_OFF_EXTRA) else 1
            conn.execute(
                "INSERT OR IGNORE INTO strategy_toggles (strategy_name, enabled) VALUES (?, ?)",
                (name, default_enabled),
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_risk_overrides (
                strategy_name TEXT PRIMARY KEY,
                stop_loss_percent REAL NOT NULL,
                take_profit_percent REAL NOT NULL
            )
        """)

        # 2026-09-03: real closed-trade ledger with REAL fill prices (not
        # the signal-time reference price the `signals` table above uses).
        # Populated from Lumibot's on_filled_order callback for main-bot
        # trades and from ib_side_channel_trader.py's own fill handling
        # for extended-hours/international trades — both write here so
        # every P&L-reporting endpoint covers the whole account without
        # separate code paths. See strategy_manager.py's on_filled_order
        # and ib_side_channel_trader.py's _handle_fill/_close_position.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS closed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                pnl REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                opened_at TEXT,
                closed_at TEXT NOT NULL,
                close_reason TEXT,
                side_channel INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_closed_trades_closed_at
            ON closed_trades(closed_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_closed_trades_strategy
            ON closed_trades(strategy)
        """)

        conn.commit()
        logger.info("Signal logging database initialized")
    finally:
        conn.close()


def log_signal(strategy_name: str, symbol: str, action: str,
               features: Dict, entry_price: Optional[float] = None,
               confirmed: Optional[bool] = None,
               timestamp: Optional[datetime] = None):
    """Log a strategy signal with its indicator context and confirmation result."""
    ts = (timestamp or datetime.now()).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO signals
               (timestamp, strategy_name, symbol, action, features, confirmed, entry_price)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, strategy_name, symbol, action, json.dumps(features),
             None if confirmed is None else int(confirmed), entry_price),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging signal for {symbol}/{strategy_name}: {e}")
    finally:
        conn.close()


def get_setting(key: str, default: float = None) -> float:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM signal_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: float):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO signal_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_settings() -> Dict[str, float]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM signal_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()


def is_strategy_enabled(strategy_name: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT enabled FROM strategy_toggles WHERE strategy_name = ?",
            (strategy_name,),
        ).fetchone()
        return bool(row["enabled"]) if row else True
    finally:
        conn.close()


def set_strategy_enabled(strategy_name: str, enabled: bool):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO strategy_toggles (strategy_name, enabled) VALUES (?, ?) "
            "ON CONFLICT(strategy_name) DO UPDATE SET enabled = excluded.enabled",
            (strategy_name, int(enabled)),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_strategy_toggles() -> Dict[str, bool]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT strategy_name, enabled FROM strategy_toggles").fetchall()
        return {row["strategy_name"]: bool(row["enabled"]) for row in rows}
    finally:
        conn.close()


def get_strategy_risk(strategy_name: str) -> Dict[str, float]:
    """Effective stop-loss/take-profit for a strategy: its own override if
    one is set, otherwise the global default. Used by BOTH the real
    bracket-order execution (_place_bracket_order) and label_signals.py's
    retrospective labeling, so the two stay consistent with each other."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT stop_loss_percent, take_profit_percent FROM strategy_risk_overrides WHERE strategy_name = ?",
            (strategy_name,),
        ).fetchone()
        if row:
            return {
                "stop_loss_percent": row["stop_loss_percent"],
                "take_profit_percent": row["take_profit_percent"],
                "is_custom": True,
            }
        return {
            "stop_loss_percent": get_setting("stop_loss_percent", 0.05),
            "take_profit_percent": get_setting("take_profit_percent", 0.10),
            "is_custom": False,
        }
    finally:
        conn.close()


def set_strategy_risk_override(strategy_name: str, stop_loss_percent: float, take_profit_percent: float):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO strategy_risk_overrides (strategy_name, stop_loss_percent, take_profit_percent) "
            "VALUES (?, ?, ?) ON CONFLICT(strategy_name) DO UPDATE SET "
            "stop_loss_percent = excluded.stop_loss_percent, take_profit_percent = excluded.take_profit_percent",
            (strategy_name, stop_loss_percent, take_profit_percent),
        )
        conn.commit()
    finally:
        conn.close()


def clear_strategy_risk_override(strategy_name: str):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM strategy_risk_overrides WHERE strategy_name = ?", (strategy_name,))
        conn.commit()
    finally:
        conn.close()


def get_all_strategy_risk() -> Dict[str, Dict[str, float]]:
    return {name: get_strategy_risk(name) for name in ALL_STRATEGY_NAMES}


def log_closed_trade(strategy: str, symbol: str, entry_price: float, exit_price: float,
                      quantity: float, close_reason: str = None,
                      opened_at: Optional[datetime] = None,
                      closed_at: Optional[datetime] = None,
                      side_channel: bool = False):
    """Record one completed round-trip trade with REAL fill prices (2026-
    09-03). This is the single source of truth for Performance Overview /
    Strategy Performance / Recent Closed Orders — both the main bot
    (strategy_manager.py's on_filled_order) and the extended-hours/
    international side channel (ib_side_channel_trader.py) write here so
    every P&L view covers the whole account. pnl/pnl_pct are computed
    here (not by callers) so every consumer agrees on the same math:
    long-only, pnl = (exit - entry) * quantity."""
    pnl = (exit_price - entry_price) * quantity
    pnl_pct = ((exit_price - entry_price) / entry_price) if entry_price else 0.0
    ts_closed = (closed_at or datetime.now()).isoformat()
    ts_opened = opened_at.isoformat() if opened_at else None
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO closed_trades
               (strategy, symbol, entry_price, exit_price, quantity, pnl, pnl_pct,
                opened_at, closed_at, close_reason, side_channel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy, symbol, entry_price, exit_price, quantity, pnl, pnl_pct,
             ts_opened, ts_closed, close_reason, int(side_channel)),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging closed trade for {symbol}/{strategy}: {e}")
    finally:
        conn.close()


def get_performance_overview(day: Optional[str] = None) -> Dict:
    """Total realized P&L for one UTC calendar day (default: today, bot's
    local/UTC time — deliberately not converted to ET, matches every
    other UTC timestamp already used across this dashboard)."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total_pnl, COUNT(*) AS trade_count "
            "FROM closed_trades WHERE substr(closed_at, 1, 10) = ?",
            (day,),
        ).fetchone()
        wins = conn.execute(
            "SELECT COUNT(*) AS c FROM closed_trades WHERE substr(closed_at, 1, 10) = ? AND pnl > 0",
            (day,),
        ).fetchone()["c"]
        return {
            "date": day,
            "total_pnl": row["total_pnl"],
            "trade_count": row["trade_count"],
            "wins": wins,
            "losses": row["trade_count"] - wins,
        }
    finally:
        conn.close()


def get_strategy_performance() -> list:
    """Per-strategy realized P&L + win rate, only for strategies with at
    least one closed trade — deliberately omits strategies with zero
    rows rather than fabricating a $0.00/0% entry (matches this
    dashboard's existing "Coming soon, not fake $0" convention)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT strategy,
                      COUNT(*) AS trade_count,
                      SUM(pnl) AS total_pnl,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
               FROM closed_trades
               GROUP BY strategy
               ORDER BY total_pnl DESC"""
        ).fetchall()
        return [
            {
                "strategy": row["strategy"],
                "trade_count": row["trade_count"],
                "total_pnl": row["total_pnl"],
                "win_rate": (row["wins"] / row["trade_count"]) if row["trade_count"] else None,
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_recent_closed_orders(limit: int = 20) -> list:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT strategy, symbol, entry_price, exit_price, quantity, pnl, pnl_pct, "
            "closed_at, close_reason, side_channel FROM closed_trades "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
