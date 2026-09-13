"""Shared regime-routed candidate pool (2026-09-09, explicit user
request): replaces each of the 8 strategies' own separate, mostly-
overlapping symbol universe with ONE shared pool — this account's real
capital is too small to justify maintaining 8 different lists.

Pipeline, exactly as directed (rewritten same day, second revision — see
below for why):
  1. Discover candidates LIVE via IB's own scanner (scan_symbols below) —
     no fixed/hardcoded symbol list anywhere in this pipeline.
  2. Classify each candidate's CURRENT regime (regime_matcher.
     classify_regime — real ADX/trend read, not a guess).
  3. Route it to whichever strategy(ies) that regime is assigned to
     (regime_matcher.STRATEGY_REGIMES) — a symbol whose regime doesn't
     match anything just gets dropped for this cycle ("noise").
  4. The strategy receiving it still runs its OWN specific entry trigger
     and its OWN stop-loss/take-profit — this only changes which symbols
     a strategy even looks at, nothing about how it decides to trade one.

2026-09-09, second revision, explicit user correction: the first version
of this pipeline used a hardcoded SHARED_CANDIDATE_POOL (20 symbols
hand-picked from a live price/volume check earlier the same day) as step
1, screened by low_cost_screen.py's own day-bar re-fetch. User's own
words: "don't keep a fix universe the price we change probably each day
or a week let this screener find the symbol." A hardcoded list is exactly
the same staleness problem the old per-strategy universes had, just moved
up one layer — confirmed same-day: a list built from 4-day-old research
was ALREADY entirely priced out by the time it got used. Replaced with a
LIVE IB scanner (ib_connector.IBConnector.scan_symbols, reqScannerSub
scription under the hood) that discovers whatever currently matches the
price/volume criteria, fresh every refresh — no list to go stale. This
also retires low_cost_screen.py from this pipeline entirely: IB's scanner
already applies price/volume filters server-side against its own live
data, so a second day-bar re-fetch afterward was redundant work (and was
the direct source of two real bugs found the same day — an unthrottled
pacing violation, and a threshold calibrated against the wrong volume
scale — see low_cost_screen.py's own comments for that history; it's
left in the repo for reference/other callers, just no longer used here).

Cached (CACHE_TTL_SECONDS) since classifying regime for every candidate
needs a real 90-day bar fetch per symbol — expensive enough to do once
per cycle across ALL strategies, not once per strategy.

Caveat, on the record: regime_matcher.py's own STRATEGY_REGIMES notes
breakout and market_profile were VALIDATED to perform BETTER outside
their nominal "bullish" label (see REGIME_PREFILTER_STRATEGIES's own
comment) — routing them through hard regime-matching here overrides
that finding. Applied anyway per explicit instruction, same "find out
live" spirit as the take-profit change earlier today — not silently
reconciled, flagged here so it isn't mistaken for an oversight.
"""
import time
import json
import os
import logging

from regime_matcher import classify_regime, STRATEGY_REGIMES
from regime_indicators import compute_indicators, MIN_BARS_REQUIRED
from symbol_screener import fetch_alpaca_screener_candidates

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 1800  # 30 minutes — same cadence the old low_cost_screen cache used

# Same $10 ceiling this account has used all day (whole-share-only IB
# sizing, real buying_power ~£53 — see low_cost_screen.py's own module
# docstring for the full reasoning, unchanged here).
SCANNER_MAX_PRICE = 10.0
# 2026-09-09, same user correction: "avoid OTC/penny names" -- live-
# verified this session that belowPrice alone doesn't achieve this. A
# real order attempt on UFG ($0.72/share, market cap floor notwith
# standing) was rejected by IB itself: "No Trading Permission... this
# product is in closing-only status" -- IB's own risk desk has already
# flagged this name as too risky for new positions, exactly the "penny
# name" category being asked to avoid. $1.00 floor is a conventional,
# conservative penny-stock cutoff (well above true sub-dollar/OTC-style
# risk, still well under the $10 ceiling).
SCANNER_MIN_PRICE = 1.0
# Share-count floor, NOT a dollar-value threshold (reqScannerSubscription's
# aboveVolume is shares) -- calibrated to IB's OWN live scanner data scale,
# not a consolidated-tape source. Confirmed live 2026-09-09 via
# low_cost_screen.py's diagnostic run: IB's reqHistoricalData volume for
# genuinely liquid (real $9M-180M/day per yfinance) sub-$10 names came
# back at roughly 100K-2M shares/day, with real names as low as ~20K-40K
# at the thin end -- 200,000 sits comfortably in the middle of that
# observed range, filtering out the thinnest names without being
# calibrated against a volume scale IB's own data never reaches. Revisit
# if live scan results come back consistently too sparse or too noisy.
SCANNER_MIN_VOLUME_SHARES = 200_000
SCANNER_NUMBER_OF_ROWS = 50
# 2026-09-09, user correction: "it is not just low cost there is more to
# it" -- the original IB portal screener this pipeline is meant to match
# filtered on Market Cap AND Price AND Volume, not price/volume alone.
# Tried wiring marketCapAbove into the live scan (any value, even a
# permissive $10M) and confirmed live it returns ZERO results every
# time, regardless of the other filters -- isolated by testing market
# cap alone against the same query that returns 50 real symbols without
# it. This isn't a threshold-tuning problem: IB's scanner-side market
# cap FIELD appears unpopulated/unusable for this account's data tier
# (delayed data only, no real-time fundamentals entitlement) rather than
# a value this account's names genuinely fail -- consistent with IB's
# own documented behavior of some scanner fields silently returning
# nothing without the right paid subscription (same shape as the
# international-location scanner finding already in project_ib_currency_
# and_scanner_findings_2026_09_09 memory). NOT used server-side for that
# reason. OTC/penny-style risk is instead covered by SCANNER_MIN_PRICE
# above (which directly, verifiably fixed the concrete case that
# surfaced this: UFG at $0.72/share, IB's own risk desk had it in
# closing-only status) and by locationCode="STK.US.MAJOR" itself (IB's
# own NYSE/NASDAQ/AMEX-only grouping -- OTC/pink-sheet names never enter
# these results to begin with). Revisit if this account's data
# entitlement ever changes.

# 2026-09-09, TOP PRIORITY per explicit user direction: a "chaos filter"
# -- excludes symbols with extreme, whipsaw-style intraday price action
# even when they're correctly regime-classified as bullish/trending.
# Found live while sandbox-testing 3 strategies from a YouTube source
# (see project_chaos_filter_priority memory for the full derivation):
# two symbols (YMAT: real ~48% crash, 114.7% 10-day range; GPRO: real
# ~3.5x whipsaw, 249.9% range) both passed a bullish/sideways regime
# check fine but wrecked every backtest they touched by dominating the
# result with one giant, chaotic move. Raw price RANGE alone is NOT a
# usable filter on its own -- it would also have excluded SUNE, the
# single best-performing symbol across all 3 strategy tests (107.5%
# range), because SUNE's big range came from a genuinely clean, mostly
# one-directional move, not chop. The real differentiator, confirmed
# across 27 live-tested symbols: EFFICIENCY RATIO (Kaufman's efficiency
# ratio -- |net price change| / sum(|bar-to-bar changes|) over the
# window, as a %). Near 100% = a straight-line trend. Near 0% = violent
# back-and-forth that nets out to almost nothing despite a huge range.
# YMAT: 0.45% efficiency. GPRO: 3.42%. SUNE (kept, correctly): 8.82%.
# Only flag on the COMBINATION of large range AND low efficiency --
# plenty of symbols have low efficiency but ALSO low range (quiet,
# boring names, not dangerous) and must NOT be excluded just for that.
CHAOS_MIN_RANGE_PCT = 50.0       # (max-min)/min over the check window, as a %
CHAOS_MAX_EFFICIENCY_PCT = 5.0   # below this + above the range threshold = flagged chaotic
# 2026-09-09, real bug caught before this ever ran live: a first version
# of this used a shorter (~2 trading day) window, on the untested
# assumption that "YMAT/GPRO-style moves happened well inside any 2-day
# window." Checked that assumption directly against GPRO's own data
# before trusting it -- FALSE. GPRO's real ~3.5x whipsaw happened in the
# first half of the 10-day validation window; its most recent 2-3 days
# alone were comparatively calm (26.6% range, well under
# CHAOS_MIN_RANGE_PCT) and would have sailed through the filter it exists
# to catch. Matches the SAME 10-day window the filter was actually
# validated against (see project_chaos_filter_priority memory) rather
# than a shorter, cheaper, unverified guess -- Lumibot's own IB data
# source (_parse_duration) needs length=~720*days for "minute" timestep
# (ceil(length/720) days after its own 2x market-hours buffer), so 7200
# requests 10 days. Real IB request cost is still exactly ONE
# reqHistoricalData call regardless of window length (more bars in the
# response, not more requests) -- already confirmed live this session
# that a 10-day/1-minute request (~3800 bars) is well within a single
# IB request's real limits.
CHAOS_CHECK_MINUTE_BARS = 7200

_cache = {"routed": {}, "last_refresh": 0.0}
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "regime_router_cache.json")


def _load_cache_from_disk():
    """2026-09-10: a watchdog restart used to wipe this cache, forcing a
    full synchronous cold re-scan (up to 50 candidates x 2 real IB
    round trips each, ~10s/symbol observed live) that itself routinely
    took longer than the watchdog's own startup grace period -- a self-
    reinforcing crash loop with no way out (see project memory,
    'regime_router/watchdog crash loop'). Persisting to disk lets a
    restart within the TTL window reuse the last good routing table
    instantly instead of re-scanning from zero, breaking the loop."""
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
        logger.warning(f"Regime router: failed to persist cache: {e}")


def _is_chaotic(strategy, symbol: str) -> bool:
    """True if `symbol`'s recent intraday price action is extreme,
    whipsaw-style chop (large range with little real net progress) --
    see the module-level comment above CHAOS_MIN_RANGE_PCT for the full
    derivation. Fail-soft: a data problem just means "can't tell,"
    treated as NOT chaotic (existing price/volume/regime filters still
    apply) rather than silently excluding a symbol for a reason
    unrelated to its actual price action."""
    try:
        bars = strategy.get_historical_prices(symbol, CHAOS_CHECK_MINUTE_BARS, "minute")
        if not bars or len(bars.df) < 30:
            return False
        closes = bars.df["close"]
        lo, hi = closes.min(), closes.max()
        if lo <= 0:
            return False
        range_pct = (hi - lo) / lo * 100
        if range_pct <= CHAOS_MIN_RANGE_PCT:
            return False  # cheap check first -- skip the efficiency math entirely if range alone already clears it
        path_length = closes.diff().abs().sum()
        if path_length <= 0:
            return False
        net_change = abs(closes.iloc[-1] - closes.iloc[0])
        efficiency_pct = net_change / path_length * 100
        return efficiency_pct < CHAOS_MAX_EFFICIENCY_PCT
    except Exception as e:
        logger.warning(f"Regime router: chaos check failed for {symbol}: {e}")
        return False


def get_symbols_for(strategy, strategy_name: str) -> list:
    """This cycle's candidate list for `strategy_name` — every symbol
    from the live-scanned pool whose CURRENT regime matches
    strategy_name's STRATEGY_REGIMES assignment. Recomputes the shared
    routing table at most once per CACHE_TTL_SECONDS (shared across every
    strategy calling this, not per-strategy), using whichever strategy
    instance calls first this cycle to do the real IB work."""
    _refresh(strategy)
    return _cache["routed"].get(strategy_name, [])


def _discover_candidates(strategy) -> list:
    """Live symbol discovery via Alpaca's own screener (most-actives +
    market movers, scans all ~13k tradable US equities server-side) --
    2026-09-10, replaces the IB reqScannerSubscription version as part
    of the move to Alpaca-only (see project memory, "Broker reversal:
    back to Alpaca-only" -- IB's per-share commission structure doesn't
    fit this account's thin scalping margins). Reuses symbol_screener.py's
    already-live fetch_alpaca_screener_candidates() rather than
    re-deriving candidate discovery from scratch. Fail-soft: returns []
    (never raises) on any failure -- a bad cycle should route nothing,
    not crash the bot."""
    try:
        raw = fetch_alpaca_screener_candidates()
        if not raw:
            return []
        prices = _get_latest_prices_alpaca(raw)
        filtered = [s for s in raw if s in prices and SCANNER_MIN_PRICE <= prices[s] <= SCANNER_MAX_PRICE]
        return filtered[:SCANNER_NUMBER_OF_ROWS]
    except Exception as e:
        logger.warning(f"Regime router: live scan failed: {e}")
        return []


def _get_latest_prices_alpaca(symbols: list) -> dict:
    """Real current prices via Alpaca's latest-quote endpoint, chunked
    (Alpaca's own request-size limits). Same pattern as
    alpaca_scalp_screener.py's get_latest_prices -- kept local here to
    avoid a regime_router -> alpaca_scalp_screener import cycle risk."""
    import os
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest

    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        return {}
    client = StockHistoricalDataClient(api_key, api_secret)
    prices = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        try:
            quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=chunk))
            for sym, q in quotes.items():
                px = q.ask_price or q.bid_price
                if px:
                    prices[sym] = float(px)
        except Exception as e:
            logger.warning(f"Regime router: latest-quote fetch failed for a chunk: {e}")
    return prices


def _refresh(strategy):
    now = time.time()
    if not _cache["routed"]:
        disk_cache = _load_cache_from_disk()
        if disk_cache is not None:
            _cache.update(disk_cache)
            logger.info(
                f"Regime router: reused disk-persisted routing table from "
                f"{now - disk_cache.get('last_refresh', 0):.0f}s ago (restart-loop guard)"
            )
    if _cache["routed"] and now - _cache["last_refresh"] < CACHE_TTL_SECONDS:
        return
    _cache["last_refresh"] = now

    candidates = _discover_candidates(strategy)

    routed = {name: [] for name in STRATEGY_REGIMES}
    for i, symbol in enumerate(candidates):
        # IB historical-data pacing limit (roughly 6 requests/2sec) —
        # confirmed live 2026-09-09 (see low_cost_screen.py's own
        # comment on the same issue): an unthrottled loop here gets
        # every request past the first few silently starved (a zero-row
        # response, not an exception), so this must stay throttled even
        # though the candidate count is now scanner-bounded (<=
        # SCANNER_NUMBER_OF_ROWS), not list-length-bounded.
        if i > 0:
            time.sleep(0.35)
        try:
            bars = strategy.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
            if not bars or len(bars.df) < MIN_BARS_REQUIRED:
                continue
            df = compute_indicators(bars.df)
            regime, _adx = classify_regime(df)
            if regime is None:
                continue
            # Chaos check only runs for symbols that already cleared
            # price/volume/regime -- deliberately the LAST, most
            # expensive filter (its own extra IB request), not the
            # first, so it never wastes a request on a symbol that was
            # going to be dropped anyway. Same pacing reasoning as the
            # main loop's own throttle -- a second real request per
            # symbol, so pause again before making it.
            time.sleep(0.35)
            if _is_chaotic(strategy, symbol):
                logger.info(f"Regime router: {symbol} passed regime check but flagged chaotic (extreme low-efficiency range), excluding")
                continue
            for strategy_name, assigned_regime in STRATEGY_REGIMES.items():
                if assigned_regime == regime:
                    routed[strategy_name].append(symbol)
        except Exception as e:
            logger.warning(f"Regime router: error classifying {symbol}: {e}")

    _cache["routed"] = routed
    _save_cache_to_disk()
    logger.info(
        f"Regime router refreshed ({len(candidates)} live-scanned candidates): " +
        ", ".join(f"{name}={len(syms)}" for name, syms in routed.items())
    )
