"""Triple-barrier labeling: walks logged signals and determines whether each
one hit its take-profit, stop-loss, or timed out.

For mean_reversion/vwap/reversal — the three strategies with their own
indicator-derived dynamic exit (see strategy_manager.py's get_exit_levels()
hook) — this reconstructs the REAL exit levels used at signal time by
recomputing regime_indicators on the historical bars up to that signal's
date, using the exact same formulas those strategies' live analyze()
methods use. Everything else uses the configured flat percentage
(signal_logger.get_all_strategy_risk), same as before.

Fixed 2026-09-01 (see project memory project-label-signals-dynamic-exit-bug):
this previously ALWAYS used the flat percentage, even for the three dynamic-
exit strategies — silently mislabeling every one of their signals against
the wrong barrier ever since the 2026-08-31 dynamic-exit deploy. Also now
respects each strategy's own time_limit_days__{strategy} override (e.g.
mean_reversion's 10 days) instead of always using the global default.

Price history comes from yfinance directly, matching what
YahooDataBacktesting (Lumibot's backtest data source, Yahoo Finance under
the hood) and live paper trading both ultimately source from — this is
Yahoo Finance price data, not Alpaca; today's Alpaca switch (see
news_sentiment.py) was scoped only to sentiment/news, not price bars.

Run this after a backtest (or periodically against live data) to turn raw
logged signals into labeled training examples for the meta-model.

Usage:
    python label_signals.py
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

from signal_logger import get_connection, get_setting, get_all_strategy_risk, init_db
from regime_indicators import (
    compute_indicators, breakout_stop_and_target, gap_fade_stop_and_target,
    MIN_BARS_REQUIRED,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Strategies with their own indicator-derived dynamic exit — see
# strategy_manager.py's get_exit_levels() hook. Anything else uses the flat
# per-strategy percentage (get_all_strategy_risk), same as before this fix.
DYNAMIC_EXIT_STRATEGIES = {"mean_reversion", "vwap", "reversal"}

INDICATOR_LOOKBACK_DAYS = 160  # calendar-day buffer for >= MIN_BARS_REQUIRED trading days


def _fetch_full_history(symbol: str, start_date: datetime, end_date: datetime):
    """Full daily OHLCV history for a symbol — needs open/volume too (not
    just high/low/close), since regime_indicators.compute_indicators uses
    them (gap_pct, volume_avg20, etc). Covers both the backward lookback
    (for indicator reconstruction) and forward window (for the triple-
    barrier walk) in one fetch per symbol.
    """
    try:
        import yfinance as yf
        df = yf.download(
            symbol,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            return None

        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]
        return df

    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
        return None


def _dynamic_exit_levels(strategy_name: str, full_history: pd.DataFrame, entry_date, entry_price: float):
    """Reconstructs what get_exit_levels() would have returned at signal
    time, by recomputing regime_indicators on the historical bars up to and
    including entry_date — the exact same formulas mean_reversion.py/
    vwap.py/reversal.py use live. Returns None if there's insufficient
    history, the relevant indicator is NaN, or the levels come out
    degenerate (mirrors _place_bracket_order's own guard) — callers fall
    back to the flat percentage in any of those cases.
    """
    history_to_date = full_history[full_history["date"].dt.date <= entry_date]
    if len(history_to_date) < MIN_BARS_REQUIRED:
        return None

    indicators = compute_indicators(history_to_date)
    row = indicators.iloc[-1]

    try:
        if strategy_name == "mean_reversion":
            if pd.isna(row[["bb_lower", "atr", "sma20"]]).any():
                return None
            stop_price, target_price = float(row["bb_lower"] - row["atr"]), float(row["sma20"])
        elif strategy_name == "vwap":
            if pd.isna(row[["vwap_upper", "atr"]]).any():
                return None
            stop_price, target_price = breakout_stop_and_target(row, entry_price)
        elif strategy_name == "reversal":
            if pd.isna(row[["atr"]]).any():
                return None
            stop_price, target_price = gap_fade_stop_and_target(row, entry_price)
        else:
            return None
    except (KeyError, TypeError):
        return None

    # Same degenerate-setup guard _place_bracket_order applies live — if the
    # reconstructed levels don't actually bracket entry_price, they're not
    # usable (can happen if price moved between when analyze() ran and the
    # stored entry_price was captured).
    if target_price <= entry_price or stop_price >= entry_price:
        return None

    return stop_price, target_price


def label_unlabeled_signals():
    """Label every BUY signal that doesn't have an outcome yet, using the
    triple-barrier method — real per-signal dynamic exit levels for
    mean_reversion/vwap/reversal (see _dynamic_exit_levels), the configured
    flat percentage for everything else. SELL signals are never labeled
    (they close positions, no forward-looking outcome to evaluate).
    """
    init_db()
    risk_by_strategy = get_all_strategy_risk()
    global_time_limit_days = get_setting("time_limit_days", 5.0)

    logger.info(
        f"Labeling with per-strategy dynamic exits where available "
        f"({sorted(DYNAMIC_EXIT_STRATEGIES)}), flat stop-loss/take-profit "
        f"otherwise, per-strategy time_limit overrides respected"
    )

    conn = get_connection()
    try:
        unlabeled = conn.execute(
            "SELECT * FROM signals WHERE outcome_label IS NULL AND entry_price IS NOT NULL AND action = 'BUY'"
        ).fetchall()
        logger.info(f"Found {len(unlabeled)} unlabeled BUY signals")

        strategy_names = {row["strategy_name"] for row in unlabeled}
        time_limit_by_strategy = {
            name: get_setting(f"time_limit_days__{name}", global_time_limit_days)
            for name in strategy_names
        }

        # Group by symbol so each symbol's price history is fetched ONCE,
        # covering every one of its signals — not once per signal.
        by_symbol = defaultdict(list)
        for row in unlabeled:
            by_symbol[row["symbol"]].append(row)

        labeled_count = 0
        skipped_count = 0
        now = datetime.now()

        for symbol, rows in by_symbol.items():
            entry_times = []
            for row in rows:
                et = datetime.fromisoformat(row["timestamp"])
                if et.tzinfo is not None:
                    et = et.replace(tzinfo=None)
                entry_times.append(et)

            max_time_limit = max(time_limit_by_strategy[row["strategy_name"]] for row in rows)
            fetch_start = min(entry_times) - timedelta(days=INDICATOR_LOOKBACK_DAYS)
            fetch_end = max(entry_times) + timedelta(days=max_time_limit + 5)
            full_history = _fetch_full_history(symbol, fetch_start, fetch_end)
            if full_history is None or len(full_history) == 0:
                skipped_count += len(rows)
                continue

            for row, entry_time in zip(rows, entry_times):
                entry_price = row["entry_price"]
                strategy_name = row["strategy_name"]
                time_limit_days = time_limit_by_strategy[strategy_name]

                end_date = entry_time + timedelta(days=time_limit_days + 5)
                if end_date > now:
                    # Not enough time has passed yet to know the outcome —
                    # leave unlabeled, picked up on a future run.
                    skipped_count += 1
                    continue

                forward_bars = full_history[
                    (full_history["date"] > entry_time) & (full_history["date"] <= end_date)
                ]
                if len(forward_bars) == 0:
                    skipped_count += 1
                    continue

                levels = None
                if strategy_name in DYNAMIC_EXIT_STRATEGIES:
                    levels = _dynamic_exit_levels(strategy_name, full_history, entry_time.date(), entry_price)

                if levels is not None:
                    stop_loss_price, take_profit_price = levels
                else:
                    risk = risk_by_strategy.get(
                        strategy_name, {"stop_loss_percent": 0.05, "take_profit_percent": 0.10}
                    )
                    take_profit_price = entry_price * (1 + risk["take_profit_percent"])
                    stop_loss_price = entry_price * (1 - risk["stop_loss_percent"])

                label = None
                exit_price = None
                exit_reason = None
                for _, bar in forward_bars.iterrows():
                    if bar["high"] >= take_profit_price:
                        label, exit_price, exit_reason = 1, take_profit_price, "take_profit"
                        break
                    if bar["low"] <= stop_loss_price:
                        label, exit_price, exit_reason = 0, stop_loss_price, "stop_loss"
                        break

                if label is None:
                    final_close = float(forward_bars.iloc[-1]["close"])
                    exit_price = final_close
                    exit_reason = "time_limit"
                    label = 1 if final_close > entry_price else 0

                conn.execute(
                    """UPDATE signals SET outcome_label = ?, outcome_computed_at = ?,
                       exit_reason = ?, exit_price = ? WHERE id = ?""",
                    (label, datetime.now().isoformat(), exit_reason, exit_price, row["id"]),
                )
                labeled_count += 1

        conn.commit()
        logger.info(
            f"Labeling complete: {labeled_count} labeled, "
            f"{skipped_count} skipped (too recent or no data)"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    label_unlabeled_signals()
