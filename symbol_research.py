"""LLM-based liquidity/trap research rider (2026-09-13, explicit user
request) for the 11 of 14 extended strategies that get zero chaos/regime
filtering today (regime_router.py's chaos filter was measured to help
only 4/16 strategies and hurt 12 — see project memory — so this targets
exactly the gap that deterministic filtering left, rather than adding a
second layer on top of strategies it already helps).

Design, exactly as directed:
  - Targets hourly_universe_screener.py's 15-symbol rotating universe —
    the same pool those 11 strategies already scan every cycle.
  - Event-driven on universe ROTATION, not a fixed daily/hourly batch:
    the underlying universe itself turns over roughly hourly (see that
    module's CACHE_TTL_SECONDS), so a fixed schedule would either waste
    calls re-researching symbols still covered, or go stale mid-session.
    main() is meant to run frequently (every 5-10 min via its own cron
    entry, scripts/run_symbol_research.sh) and diffs the CURRENT universe
    against what's already researched and still fresh — only symbols that
    actually changed get a real (slow, real-token-cost) research call.
    A quiet cycle with nothing new costs nothing.
  - This NEVER runs inside the live 60s trading loop — strategy_manager.py
    only ever calls get_research() here, a cheap on-disk cache read. The
    slow part (an actual `claude -p` call, real seconds) only ever
    happens in this file's own standalone cron-driven process. This
    project has a real history of iteration-time stalls from slow
    external calls (see watchdog.sh's own STALL_THRESHOLD_SECONDS
    history) — keeping the LLM call fully out-of-band is deliberate, not
    an oversight.
  - Consumed on BOTH sides of a trade's life, per explicit user
    direction — not just as an entry gate:
      1. Entry gate + take-profit widening at entry time
         (strategy_manager.py's _run_strategy).
      2. A late-arriving verdict acts on an ALREADY-OPEN position too —
         if research was still running when a position opened and later
         comes back flagging a trap, the position gets closed immediately
         regardless of current P&L (green, mildly down, or badly down —
         user's own reasoning: a confirmed trap means exit either way,
         not "wait and see"). If it instead confirms a genuine large
         move, the position's take-profit gets widened in place, same
         mechanism strategy_manager.py's own priority-takeover system
         already uses (bracket.take_profit_price reassignment — take-
         profit is checked in software every cycle, not a resting broker
         order, so this needs no order cancel/replace).
         See strategy_manager.py's _check_symbol_research_exits_and_tp_updates.
  - Fails open everywhere, same philosophy as every other rider in this
    project (asset_class_riders.py): no cache file yet, a symbol not
    covered, a stale verdict, a parse failure, an API error — any of
    these just means normal entry/exit logic proceeds completely
    unchanged. This module is never allowed to be a reason a trade didn't
    happen or didn't exit on its own normal terms.

Research itself is a single `claude -p` call per symbol with NO tool
access (no --dangerously-skip-permissions, no Bash) — real recent price/
volume data is fetched here in Python and embedded directly in the
prompt, so this is a pure reasoning call, not an agentic session. Framed
around ICT/Smart Money Concepts (liquidity sweeps, stop-hunts, fair
value gaps) since that's the lens this was asked to use — ask Claude to
respond with nothing but a JSON object, parsed directly from stdout.
"""
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
# Runs standalone via cron (scripts/run_symbol_research.sh), unlike
# strategy_manager.py which only ever runs already-imported into
# bot_runner.py (which loads .env itself) -- nothing else loads .env for
# THIS process, so it has to do it itself. Found live: without this,
# ALPACA_API_KEY/SECRET are None here even though .env has them, and
# StockHistoricalDataClient fails with "You must supply a method of
# authentication" -- silently caught by _fetch_recent_bars' own
# try/except, surfacing only as an odd "not enough bar data" everywhere.
load_dotenv()

import config

logger = logging.getLogger(__name__)

# The 11 of 14 extended strategies with use_regime_chaos_filter left at
# its default False (see extended_strategies_live.py) — the other 3
# (ema_ribbon_smi, volume_divergence_grab, candle_taxonomy) already get
# real filtering and aren't this module's target.
RESEARCH_TARGET_STRATEGIES = frozenset({
    "lab_range", "supply_demand", "supertrend_200ema", "fakeout_breakout_fib",
    "macd_200ema", "discount_zone", "breakout_chandelier", "asymmetric_dual",
    "rigorous_rr", "volume_absorption", "volume_profile",
})

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbol_research_cache.json")


def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict):
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except OSError as e:
        logger.warning(f"symbol_research: failed to persist cache: {e}")


def get_research(symbol: str) -> dict | None:
    """Cheap, on-disk read — the ONLY thing strategy_manager.py ever calls
    directly, every live cycle. Returns None (never raises) if there's no
    verdict, it's stale, or anything about the cache is malformed — every
    caller must already treat None as "no opinion, proceed normally,"
    same convention as every asset_class_riders.py check."""
    if not getattr(config, "ENABLE_SYMBOL_RESEARCH_RIDER", False):
        return None
    try:
        entry = _load_cache().get(symbol)
        if not entry:
            return None
        age = time.time() - entry.get("researched_at", 0)
        if age > config.SYMBOL_RESEARCH_FRESHNESS_SECONDS:
            return None
        return entry
    except Exception as e:
        logger.warning(f"symbol_research: get_research failed for {symbol}: {e}")
        return None


def _fetch_recent_bars(symbol: str, days: int = 15) -> list:
    """Real recent daily OHLCV, oldest->newest, as plain dicts — embedded
    directly in the research prompt. Same Alpaca data-client pattern
    regime_router.py's own _get_latest_prices_alpaca already uses in this
    project. Returns [] (never raises) on any failure — the caller must
    treat that as "not enough to research," not crash the batch."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        api_key = os.environ.get("ALPACA_API_KEY")
        api_secret = os.environ.get("ALPACA_API_SECRET")
        if not api_key or not api_secret:
            return []
        client = StockHistoricalDataClient(api_key, api_secret)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=days * 2),  # buffer for weekends/holidays
        )
        bars = client.get_stock_bars(req)
        rows = bars.data.get(symbol, [])[-days:]
        return [
            {
                "date": b.timestamp.date().isoformat(),
                "open": round(float(b.open), 4),
                "high": round(float(b.high), 4),
                "low": round(float(b.low), 4),
                "close": round(float(b.close), 4),
                "volume": int(b.volume),
            }
            for b in rows
        ]
    except Exception as e:
        logger.warning(f"symbol_research: bar fetch failed for {symbol}: {e}")
        return []


_RESEARCH_PROMPT_TEMPLATE = """You are doing pure market-structure research on one stock symbol, using \
Smart Money Concepts / ICT-style reasoning (liquidity sweeps, stop-hunts, fair value gaps, \
genuine breakouts vs. traps). You have no tools — reason only from the real recent daily \
bars given below.

Symbol: {symbol}

Recent daily bars (oldest to newest, JSON):
{bars_json}

Answer two questions from this data alone:
1. Does the most recent price action look like a liquidity trap / stop-hunt setup (a sharp \
move likely to reverse and catch late entries), or does it look like a genuine, sustainable \
move?
2. If genuine, roughly how large a continuation move (as a multiple of a normal ~1.5:1 \
reward:risk target) would you realistically expect — be conservative, this feeds a real \
position-sizing decision, not a headline number.

Respond with ONLY a JSON object, no other text, matching exactly this shape:
{{"trap_flag": true or false, "predicted_move_multiplier": a number (1.0 if no real edge either way, higher only with real conviction), "reasoning": "ONE short sentence, under 160 characters, plain language"}}
"""


def _research_symbol(symbol: str) -> dict | None:
    """One `claude -p` call, no tool access — real bar data is already
    embedded in the prompt text, so this is pure reasoning, not an
    agentic session (no --dangerously-skip-permissions needed). Returns
    None on any failure (timeout, bad JSON, empty output) rather than
    caching a bad result — a missing verdict fails open exactly like a
    stale one does."""
    bars = _fetch_recent_bars(symbol)
    if len(bars) < 5:
        logger.info(f"symbol_research: not enough bar data for {symbol}, skipping")
        return None

    prompt = _RESEARCH_PROMPT_TEMPLATE.format(symbol=symbol, bars_json=json.dumps(bars))

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True,
            timeout=config.SYMBOL_RESEARCH_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning(f"symbol_research: claude -p call failed for {symbol}: {e}")
        return None

    if result.returncode != 0 or not result.stdout.strip():
        logger.warning(f"symbol_research: claude -p returned nothing usable for {symbol} (exit {result.returncode})")
        return None

    raw = result.stdout.strip()
    # Strip a markdown code fence if the model wrapped its JSON in one
    # despite being asked not to — cheap defensive parsing, not a retry loop.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        verdict = json.loads(raw)
        if "trap_flag" not in verdict or "predicted_move_multiplier" not in verdict:
            raise ValueError("missing required keys")
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"symbol_research: unparseable verdict for {symbol}: {e} — raw: {raw[:200]}")
        return None

    return {
        "trap_flag": bool(verdict["trap_flag"]),
        "predicted_move_multiplier": float(verdict["predicted_move_multiplier"]),
        "reasoning": str(verdict.get("reasoning", "")),
        "researched_at": time.time(),
    }


def main():
    """Cron entry point (scripts/run_symbol_research.sh, every 5-10 min).
    Cheap by design: only symbols currently in the rotating universe AND
    not already fresh in cache get a real (slow) research call. A quiet
    cycle where nothing changed does zero LLM calls."""
    import hourly_universe_screener

    class _StandaloneStrategy:
        """hourly_universe_screener.get_rotating_universe(strategy) only
        ever calls strategy.get_historical_prices — this script has no
        live Strategy instance, so it reads the screener's own disk cache
        directly instead of forcing a fresh scan (which needs a real
        Strategy object it doesn't have). If the screener hasn't cached
        anything yet, there's nothing to research this cycle — fine, the
        live bot's own cycles will populate it soon enough."""
        pass

    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hourly_universe_cache.json")
    try:
        with open(cache_path) as f:
            universe = json.load(f).get("universe", [])
    except (FileNotFoundError, json.JSONDecodeError):
        print("symbol_research: no hourly_universe_cache.json yet, nothing to do this cycle")
        return

    if not universe:
        print("symbol_research: rotating universe is currently empty, nothing to do")
        return

    cache = _load_cache()
    now = time.time()
    needs_research = [
        s for s in universe
        if s not in cache or (now - cache[s].get("researched_at", 0)) > config.SYMBOL_RESEARCH_FRESHNESS_SECONDS
    ]

    if not needs_research:
        print(f"symbol_research: all {len(universe)} current symbols already fresh, nothing to do")
        return

    print(f"symbol_research: researching {len(needs_research)} of {len(universe)} symbols: {needs_research}")
    researched_count = 0
    for symbol in needs_research:
        verdict = _research_symbol(symbol)
        if verdict is not None:
            cache[symbol] = verdict
            researched_count += 1
            print(f"  {symbol}: trap_flag={verdict['trap_flag']} predicted_move_multiplier={verdict['predicted_move_multiplier']}")

    # Prune anything no longer in the universe AND stale — keeps the
    # cache file from growing unbounded over many rotations, while still
    # letting a symbol that's fresh AND currently out of the universe
    # stick around in case it rotates back in soon.
    cache = {
        s: v for s, v in cache.items()
        if s in universe or (now - v.get("researched_at", 0)) <= config.SYMBOL_RESEARCH_FRESHNESS_SECONDS
    }
    _save_cache(cache)
    print(f"symbol_research: done, {researched_count}/{len(needs_research)} succeeded, cache now has {len(cache)} entries")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
