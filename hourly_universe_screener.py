"""Live hourly bullish + high-intraday-volatility rotating universe
(2026-09-11), built for the 14 new strategies added this session
(extended_strategies_live.py). Distinct from regime_router.py's ADX-
regime-based routing (which the original 8 strategies use) -- this is a
different selection criterion (momentum + volatility, not trend regime),
so it's a sibling module, not a replacement.

Live port of the research done in
sandbox_hourly_bullish_volatility_universe.py this session:
  - "Bullish this hour": MACD line above signal line, RSI > 50, and this
    hour's total volume above what the 20-bar rolling average volume
    would predict for that many bars -- the user's own answer to "how
    should bullish be measured," given directly this session.
  - "High intraday volatility": ATR% (ATR / close * 100) -- the user's
    own answer to "how should volatility be measured." Bullish candidates
    are ranked by ATR% descending; the top TARGET_LIST_SIZE become this
    hour's rotating universe.

Same architectural pattern as regime_router.py: candidate discovery reused
from symbol_screener.py (no separate/duplicate discovery logic), TTL-
cached (shared across every strategy calling this, not re-computed per
strategy), disk-persisted so a restart within the TTL window doesn't
force an expensive cold recompute (same restart-loop guard reasoning as
regime_router.py), fail-soft (a data problem routes nothing that cycle,
never crashes the bot).
"""
import time
import json
import os
import logging

import numpy as np
import pandas as pd

from symbol_screener import fetch_alpaca_screener_candidates, quality_filter_symbols

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600  # hourly, per this screener's own design (vs regime_router's 30 min)

MIN_PRICE = 1.0
CANDIDATE_POOL_SIZE = 100
TARGET_LIST_SIZE = 15

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
VOLUME_AVG_PERIOD = 20
ATR_PERIOD = 14
LOOKBACK_MINUTE_BARS = 600  # enough for a 26-period MACD + 20-period volume avg with room to spare

# Same leveraged/sector ETF seed list as the sandbox research -- Alpaca's
# screener has no ETF-specific mover category, so these are seeded in
# directly rather than only relying on stock movers.
ETF_SEED_LIST = [
    "SOXL", "SOXS", "TNA", "TZA", "GDX", "GDXJ", "LABU", "LABD", "XBI",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UVXY", "FAS", "FAZ", "NUGT", "DUST",
]

_cache = {"universe": [], "last_refresh": 0.0}
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "hourly_universe_cache.json")


def _load_cache_from_disk():
    try:
        with open(_CACHE_FILE) as f:
            data = json.load(f)
        if time.time() - data.get("last_refresh", 0) < CACHE_TTL_SECONDS:
            return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def _save_cache_to_disk():
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(_cache, f)
    except OSError as e:
        logger.warning(f"Hourly universe screener: failed to persist cache: {e}")


def _compute_macd(close):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return macd_line, signal_line


def _compute_rsi(close, period=RSI_PERIOD):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _compute_atr_pct(df, period=ATR_PERIOD):
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return (atr / df["close"]) * 100


def _is_bullish_and_volatile(strategy, symbol):
    """(is_bullish, atr_pct) for `symbol` off its most recent minute bars,
    or (False, None) on any data problem -- fail-soft, same convention as
    regime_router._is_chaotic."""
    try:
        bars = strategy.get_historical_prices(symbol, LOOKBACK_MINUTE_BARS, "minute")
        if not bars or len(bars.df) < MACD_SLOW + MACD_SIGNAL + 5:
            return False, None
        df = bars.df
        macd_line, signal_line = _compute_macd(df["close"])
        rsi = _compute_rsi(df["close"])
        vol_avg = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()
        atr_pct = _compute_atr_pct(df)

        last = df.iloc[-1]
        last_macd, last_signal = macd_line.iloc[-1], signal_line.iloc[-1]
        last_rsi = rsi.iloc[-1]
        last_vol_avg = vol_avg.iloc[-1]
        last_atr_pct = atr_pct.iloc[-1]
        if pd.isna(last_macd) or pd.isna(last_signal) or pd.isna(last_rsi) or pd.isna(last_vol_avg) or pd.isna(last_atr_pct):
            return False, None

        # This hour's window: bars since the top of the current clock hour.
        hour_start = df.index[-1].floor("h")
        window = df[df.index >= hour_start]
        hour_volume = float(window["volume"].sum())
        volume_confirmed = hour_volume > last_vol_avg * len(window) if len(window) else False

        bullish = bool(last_macd > last_signal and last_rsi > 50 and volume_confirmed)
        return bullish, float(last_atr_pct)
    except Exception as e:
        logger.warning(f"Hourly universe screener: check failed for {symbol}: {e}")
        return False, None


def _discover_candidate_pool():
    """Same discovery pattern as regime_router._discover_candidates and
    the original sandbox research: Alpaca's live most-actives/movers
    screener, plus the leveraged/sector ETF seed list, quality-filtered,
    capped to CANDIDATE_POOL_SIZE. Fail-soft: [] on any failure."""
    try:
        raw = fetch_alpaca_screener_candidates()
        pool = list(set(raw) | set(ETF_SEED_LIST))
        pool = quality_filter_symbols(pool)
        return pool[:CANDIDATE_POOL_SIZE]
    except Exception as e:
        logger.warning(f"Hourly universe screener: candidate pool discovery failed: {e}")
        return []


def get_rotating_universe(strategy) -> list:
    """This hour's top-TARGET_LIST_SIZE bullish+volatile symbols. Cached
    at most once per CACHE_TTL_SECONDS, shared across every strategy that
    calls this (not recomputed per-strategy) -- same sharing convention
    as regime_router.get_symbols_for."""
    _refresh(strategy)
    return list(_cache["universe"])


def _refresh(strategy):
    now = time.time()
    if not _cache["universe"]:
        disk_cache = _load_cache_from_disk()
        if disk_cache is not None:
            _cache.update(disk_cache)
            logger.info(
                f"Hourly universe screener: reused disk-persisted list from "
                f"{now - disk_cache.get('last_refresh', 0):.0f}s ago (restart-loop guard)"
            )
    if _cache["universe"] and now - _cache["last_refresh"] < CACHE_TTL_SECONDS:
        return
    _cache["last_refresh"] = now

    pool = _discover_candidate_pool()
    scored = []
    for i, symbol in enumerate(pool):
        bullish, atr_pct = _is_bullish_and_volatile(strategy, symbol)
        if bullish and atr_pct is not None:
            scored.append((symbol, atr_pct))

    scored.sort(key=lambda kv: kv[1], reverse=True)
    universe = [s for s, _ in scored[:TARGET_LIST_SIZE]]

    _cache["universe"] = universe
    _save_cache_to_disk()
    logger.info(f"Hourly universe screener refreshed ({len(pool)} pool candidates): {len(universe)} bullish+volatile symbols: {universe}")
