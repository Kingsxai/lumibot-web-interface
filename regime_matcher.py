"""Matches a symbol's current market regime (sideways/bullish/bearish) to
the best-fit strategy already in this codebase.

Piece 2 of the regime-classification idea discussed 2026-08-31 (piece 1:
classify a symbol's regime — see regime_indicators.py's ADX logic, already
used live by mean_reversion/vwap/reversal; piece 2: this file, matching that
regime to the best-fit existing strategy; piece 3, parked: AI-generate a new
strategy in sandbox for symbols with no fit).

The mapping below is drawn from what's actually validated in this codebase
(experimental_bb_momentum_hedge.py + project_strategy_version_decisions
memory), not a general theory:
  sideways (range-bound, ADX < ADX_RANGE_THRESHOLD): mean_reversion is the
    only strategy explicitly gated on this regime (BB reversion,
    reversion_regime_ok in regime_indicators.py).
  bullish (trending up, close > sma20): vwap's VWAP+Breakout — the
    broadest-winning trend-following entry tested (profitable across 6
    asset classes: index ETFs, large-caps, commodities, growth stocks,
    crypto, futures).
  bearish (trending down, close < sma20): reversal's gap-fade — the only
    strategy specifically validated on down-moves (76.9% win rate on
    large-caps in the sandbox).
Trending strategies without a distinct validated asset-class edge
(breakout, momentum) aren't in this map — they're not a clearly "better"
fit for bullish/bearish than vwap/reversal on the data we actually have.
"""
import logging
from regime_indicators import compute_indicators, ADX_RANGE_THRESHOLD, MIN_BARS_REQUIRED

logger = logging.getLogger(__name__)

REGIME_STRATEGY_MAP = {
    "sideways": "mean_reversion",
    "bullish": "vwap",
    "bearish": "reversal",
}

# Regime assignment for ALL 8 strategies (2026-09-01), used by
# strategy_manager.py's live pre-entry regime gate — a strategy may only
# open a new position on a symbol whose CURRENT regime matches its entry
# here; a regime mismatch blocks the order (see _check_regime_gate).
#
# mean_reversion/vwap/reversal: unchanged from REGIME_STRATEGY_MAP above —
# sandbox-validated (experimental_bb_momentum_hedge.py).
#
# momentum/breakout/gap_and_go/scalping/market_profile: 2026-09-02 —
# properly validated (see regime_validation_daily.py / regime_validation_
# intraday.py, real historical data, real entry/exit logic, no lookahead —
# not the structural guess this used to be). Real per-symbol win-rate/
# avg-return comparison, bullish-regime signals vs everything else:
#   momentum:    bullish n=394 win=52.0% ret=+0.89% | other n=28  win=53.6% ret=+2.06%
#                Inconclusive — non-bullish sample is too small to trust
#                (momentum only buys already-strong gainers, which are
#                usually already in a bullish regime by construction: 93%
#                of its real signals land there). "bullish" kept as
#                assignment since it's overwhelmingly where signals occur
#                anyway; see REGIME_PREFILTER_STRATEGIES below for how
#                this is actually used live.
#   breakout:    bullish n=174 win=54.0% ret=+0.62% | other n=65  win=56.9% ret=+1.50%
#                REFUTED — non-bullish regime signals actually performed
#                BETTER on both win rate and return. "bullish" stays as
#                the label (still used by the same-cycle precedence tie-
#                breaker below) but breakout is deliberately EXCLUDED from
#                the hard pre-filter — filtering here would cut its
#                better-performing trades and keep its worse ones.
#   gap_and_go:  bullish n=84  win=47.6% ret=+0.84% | other n=58  win=36.2% ret=-0.43%
#                VALIDATED — real, meaningful edge in the bullish regime
#                (non-bullish signals were net-losing on average).
#   scalping:    bullish n=526 win=43.9% ret=+0.05% | other n=3894 win=39.6% ret=+0.02%
#                VALIDATED — modest but consistent and backed by large
#                samples both sides (2x avg return, +4.3pp win rate).
#   market_profile: bullish n=91 win=23.1% ret=+0.07% | other n=147 win=25.9% ret=+0.14%
#                REFUTED — same pattern as breakout, non-bullish regime
#                signals performed better on both measures. Excluded from
#                the hard pre-filter for the same reason.
# All five entry sizes are real trade counts from that validation run, not
# placeholders — re-run periodically as more real live signal history
# accumulates, especially momentum's thin non-bullish sample.
STRATEGY_REGIMES = {
    "mean_reversion": "sideways",
    "vwap": "bullish",
    "reversal": "bearish",
    "momentum": "bullish",
    "breakout": "bullish",
    "gap_and_go": "bullish",
    "scalping": "bullish",
    "market_profile": "bullish",
    # 2026-09-11: 14 new strategies (extended_strategies_live.py). All are
    # long-only structure/momentum entries (SMC zone touches, breakouts,
    # trend-following crosses) -- "bullish" per the same majority pattern
    # the existing 6/8 already use. Heuristic/unvalidated, same caveat the
    # existing assignments already carry -- these ship seeded OFF, so this
    # only affects same-cycle precedence resolution against each other/the
    # existing 8, never gates entry on its own (see REGIME_PREFILTER_
    # STRATEGIES below, which none of these 14 are added to).
    "lab_range": "bullish",
    "supply_demand": "bullish",
    "supertrend_200ema": "bullish",
    "fakeout_breakout_fib": "bullish",
    "macd_200ema": "bullish",
    "ema_ribbon_smi": "bullish",
    "discount_zone": "bullish",
    "breakout_chandelier": "bullish",
    "volume_divergence_grab": "bullish",
    "asymmetric_dual": "bullish",
    "rigorous_rr": "bullish",
    "volume_absorption": "bullish",
    "candle_taxonomy": "bullish",
    "volume_profile": "bullish",
}

# Strategies where a regime MISMATCH is trusted enough (see the validation
# numbers above) to skip a symbol entirely BEFORE that strategy does its
# own more expensive per-symbol work (get_last_price/get_historical_prices
# calls to Interactive Brokers) — not just used as the same-cycle
# precedence tie-breaker every strategy participates in regardless.
# Added 2026-09-02 specifically to cut real, wasted IB request volume (a
# genuine contributor to the bot's periodic instability — see project
# memory) without suppressing real opportunity on unvalidated grounds:
# mean_reversion/vwap/reversal already have their own regime-consistent
# entry gates baked directly into their math (reversion_regime_ok/
# breakout_confirmed/reversal_fade_confirmed in regime_indicators.py) so
# they don't need this separate mechanism. breakout and market_profile are
# deliberately EXCLUDED — real validation above refuted "bullish" for
# both, so hard-filtering them would suppress the trades that actually
# work. See get_symbol_regime() below for the actual gate implementation.
#
# 2026-09-12: added ema_ribbon_smi, volume_divergence_grab, candle_taxonomy
# (3 of the 14 extended_strategies_live.py strategies) after a controlled
# filtered-vs-unfiltered backtest across all 16 currently-live strategies
# found this exact regime check (paired with regime_router._is_chaotic,
# both applied together in extended_strategies_live.py's
# use_regime_chaos_filter) was one of only 4/16 the filter actually
# helped. The other 11 extended strategies were deliberately NOT added —
# the same test found this filter net-hurts them, in two cases
# (supertrend_200ema, volume_absorption) turning a real profit into a
# loss.
REGIME_PREFILTER_STRATEGIES = {"momentum", "gap_and_go", "scalping",
                                "ema_ribbon_smi", "volume_divergence_grab",
                                "candle_taxonomy"}


def classify_regime(df):
    """(regime, adx) for the most recent bar of an indicator-computed
    dataframe (regime_indicators.compute_indicators output). Returns
    (None, None) if there isn't enough history for a real reading yet."""
    row = df.iloc[-1]
    if row[["adx", "sma20", "close"]].isna().any():
        return None, None
    adx = float(row["adx"])
    if adx < ADX_RANGE_THRESHOLD:
        return "sideways", adx
    return ("bullish" if row["close"] > row["sma20"] else "bearish"), adx


def symbol_regime_matches(strategy, strategy_name: str, symbol: str) -> bool:
    """True if `symbol`'s CURRENT regime matches `strategy_name`'s
    validated assignment in STRATEGY_REGIMES, for strategies in
    REGIME_PREFILTER_STRATEGIES only. Every other strategy (not in that
    set) always returns True here — unfiltered, unchanged behavior.

    Callers should check this BEFORE doing their own more expensive per-
    symbol work (get_last_price / get_historical_prices calls), to skip
    obviously-wrong-regime symbols without spending an IB request on them.
    The daily-bar fetch this needs (MIN_BARS_REQUIRED=90 days) goes
    through strategy.get_historical_prices, which strategy_manager.py
    already caches once per trading cycle — if mean_reversion/vwap/
    reversal (or another prefiltered strategy) already pulled the same
    (symbol, 90, "day") combo this cycle, this is a free cache hit, not a
    new request.
    """
    if strategy_name not in REGIME_PREFILTER_STRATEGIES:
        return True
    from regime_indicators import compute_indicators, MIN_BARS_REQUIRED
    bars = strategy.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
    if not bars or len(bars.df) < MIN_BARS_REQUIRED:
        # Not enough history for a real reading — don't block the
        # strategy's own logic over a missing regime signal, just let it
        # proceed unfiltered for this symbol this cycle.
        return True
    indicators = compute_indicators(bars.df)
    regime, adx = classify_regime(indicators)
    if regime is None:
        return True
    return regime == STRATEGY_REGIMES.get(strategy_name)


def match_symbol(strategy, symbol, universes: dict, toggles: dict):
    """Fetches real daily bars for `symbol` via a live Strategy object's
    get_historical_prices, classifies its current regime, and reports the
    best-fit strategy plus whether that strategy already covers it.

    Args:
        strategy: the running MultiStrategyBot (or any object exposing
            get_historical_prices, matching every other *.py strategy file).
        universes: {strategy_name: [symbols]}, e.g. from symbol_universes.json.
        toggles: {strategy_name: enabled}, e.g. from signal_logger.
    """
    bars = strategy.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
    if not bars or len(bars.df) < MIN_BARS_REQUIRED:
        return None

    df = compute_indicators(bars.df)
    regime, adx = classify_regime(df)
    if regime is None:
        return None

    matched = REGIME_STRATEGY_MAP[regime]
    in_universe = symbol in universes.get(matched, [])
    enabled = toggles.get(matched, False)

    return {
        "symbol": symbol,
        "regime": regime,
        "adx": round(adx, 1),
        "matched_strategy": matched,
        "in_universe": in_universe,
        "strategy_enabled": enabled,
        "already_covered": in_universe and enabled,
    }
