"""Phase 3: dynamic symbol screener (widened 2026-08-31 — see below).

Replaces the hardcoded 3-symbol universes in scalping.py / breakout.py /
mean_reversion.py / vwap.py / gap_and_go.py / reversal.py with a ranked list:
S&P 500 constituents PLUS Alpaca's own market-wide screener (most-active +
biggest movers, scanning all ~13k tradable US equities server-side —
sandbox-validated in sandbox_wider_screener.py on 2026-08-31), filtered by
tradability/warrant-exclusion, liquidity (avg dollar volume), bounded
volatility, and a name-keyword exclusion for bond/fixed-income ETFs (these
passed the liquidity/volatility filters in the sandbox run but are noise
for these strategies — see PROTECTED_SYMBOLS/EXCLUDED_NAME_KEYWORDS below
and project memory), then re-ranked by each strategy's own historical
signal quality from signals.db where we have backtest history (Phase 1
data) — falling back to the liquidity rank for symbols we haven't traded
yet.

Historical quality only exists so far for the ~10 symbols already covered by
the Phase 1 backtests (AAPL, MSFT, TSLA, AMZN, NVDA, SPY, QQQ) — everything
else is "untested" until it accumulates its own signal history from being
traded. This screener re-ranks honestly rather than pretending to know
quality it doesn't have.

Usage:
    python symbol_screener.py            # writes symbol_universes.json
"""

import json
import logging
import os

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

from signal_logger import get_connection

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLEAN_DATA_WATERMARK = 46380  # see project memory / train_meta_model.py — pre-fix data is duplicated

# Used only if the Wikipedia fetch fails (no internet, page structure change).
FALLBACK_SP500_SAMPLE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V",
    "UNH", "XOM", "JNJ", "WMT", "MA", "PG", "HD", "CVX", "MRK", "ABBV",
    "SPY", "QQQ",
]

MIN_AVG_DOLLAR_VOLUME = 50_000_000  # filter out illiquid names
MAX_ANNUALIZED_VOLATILITY = 1.2     # filter out extreme/erratic names (120%/yr)
LOOKBACK_PERIOD = "3mo"
TOP_LIQUID_N = 150                  # cap how many symbols advance past the liquidity filter (widened 2026-08-31)
TOP_K_PER_STRATEGY = 10             # widened 2026-08-31 from 3, now that the candidate pool is much bigger

# Name-keyword exclusion for asset classes these strategies were never
# validated against — applies to ALL strategies. Found via
# sandbox_full_universe_test.py's full-13k-symbol scan on 2026-08-31:
# bond/money-market/ultra-short-duration funds mechanically pass liquidity/
# volatility filters (they barely move, so they're "near their high"
# almost constantly), and alternative/derivative-strategy funds (managed
# futures, commodity strategy, currency, options-overlay) track a
# different asset class or mechanism entirely. Neither has any real
# relationship to breakout/momentum/reversal entry logic. User confirmed
# 2026-08-31: no backtest needed for this exclusion, it's asset-class fit,
# not a strategy-edge claim — implement and observe live.
EXCLUDED_NAME_KEYWORDS = (
    "bond", "treasury", "fixed income", "money market", "ultra-short", "ultra short",
    "short maturity", "t-bill", "1-3 month", "mortgage-backed", " mbs ", "clo etf",
    "senior loan", "tips", "managed futures", "commodity strategy", "dollar index",
    "covered call",
)

# Leveraged/inverse ETFs (2x/3x, "ultrashort", "ultrapro", "bear", inverse
# products) — same 2026-08-31 scan found these disproportionately fool
# `breakout` and `mean_reversion` specifically (8 and 2 hits out of their
# BUY signals respectively — low/distorted volatility mechanically trips
# their "near the high"/band-touch logic) while vwap/reversal/gap_and_go
# barely touch this category (0/0/1 hits). So this is a PER-STRATEGY
# exclusion, not blanket — see STRATEGIES_BLOCKING_LEVERAGED_INVERSE.
LEVERAGED_INVERSE_KEYWORDS = (
    "ultrashort", "ultra short", "ultrapro", "ultra pro", "ultra ", " short ", "bear ",
    "bull 3x", "bull 2x", " 2x ", " 3x ", "-1x", "-2x", "-3x", "inverse",
)
STRATEGIES_BLOCKING_LEVERAGED_INVERSE = {"breakout", "mean_reversion"}

# Symbols specifically validated for these strategies in the 2026-08-30
# sandbox rounds (experimental_bb_momentum_hedge.py) — always kept in the
# final universe regardless of liquidity-rank cutoff, so a re-run of this
# screener can't silently drop them in favor of a more-liquid-but-untested
# S&P name (per the caution already in STRATEGY_DEFAULTS below).
PROTECTED_SYMBOLS = {
    "mean_reversion": {"GLD", "USO"},
    "vwap": {"GLD", "USO"},
    "reversal": {"GLD", "USO", "GME", "AMC"},
}
MIN_TESTED_SAMPLES = 5              # need at least this many labeled BUYs to trust a symbol's win rate

STRATEGY_DEFAULTS = {
    "scalping": ["AAPL", "MSFT", "AMZN"],
    "breakout": ["AAPL", "MSFT", "TSLA"],
    # mean_reversion was replaced 2026-08-30 with a regime-gated Bollinger
    # Band entry (see regime_indicators.py), validated in
    # experimental_bb_momentum_hedge.py to only work on large-cap stocks +
    # commodity ETFs — NOT on index ETFs or growth stocks.
    # vwap was replaced 2026-08-30 (twice, same day — see project memory)
    # with VWAP+Breakout, validated as profitable across index ETFs,
    # large-caps, commodities, growth stocks, crypto, and futures (crypto/
    # futures excluded here — not tradable via the current Alpaca wiring).
    # UPDATE 2026-08-31: GLD/USO/GME/AMC are now in PROTECTED_SYMBOLS above,
    # so re-running this screener can no longer silently drop them — they're
    # unioned into the final list regardless of the liquidity-rank cutoff.
    "mean_reversion": ["AAPL", "MSFT", "JPM", "GLD", "USO"],
    "vwap": ["SPY", "QQQ", "DIA", "AAPL", "MSFT", "JPM", "GLD", "USO", "TSLA", "NVDA"],
    "gap_and_go": ["AAPL", "TSLA", "NVDA"],
    # reversal was replaced 2026-08-30 with a gap-fade entry (see
    # regime_indicators.py), validated as profitable on index ETFs,
    # large-caps, commodities, growth stocks, and small-cap hype names.
    # GME/AMC/GLD/USO protection: see PROTECTED_SYMBOLS above.
    "reversal": ["SPY", "QQQ", "DIA", "AAPL", "MSFT", "JPM", "GLD", "USO", "TSLA", "NVDA", "GME", "AMC"],
    # market_profile (2026-09-02): needs real trading volume for its
    # volume-at-price profile to be meaningful — same liquid-mega-cap
    # reasoning as scalping's own defaults. SPY removed — confirmed a
    # real loser under risk-normalized sizing in sandbox testing (see
    # market_profile.py's module docstring / experimental_market_
    # profile_filters.py), not screener-validated otherwise yet.
    "market_profile": ["AAPL", "MSFT", "TSLA", "NVDA", "QQQ"],
}


def fetch_alpaca_screener_candidates() -> list:
    """Alpaca's own market-wide screener — scans all ~13k tradable US
    equities server-side, far broader than the S&P 500 alone. Combines
    top-100 most-active (by volume) with top-50 gainers/losers. Falls back
    to an empty list (not a crash) if the API call fails for any reason —
    the S&P 500 list alone is still a reasonable candidate pool on its own.
    """
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MostActivesRequest, MarketMoversRequest
        from alpaca.data.enums import MostActivesBy, MarketType

        api_key = os.environ.get("ALPACA_API_KEY")
        api_secret = os.environ.get("ALPACA_API_SECRET")
        if not api_key or not api_secret:
            logger.warning("No Alpaca credentials, skipping Alpaca screener candidates")
            return []

        client = ScreenerClient(api_key, api_secret)
        symbols = set()

        actives = client.get_most_actives(MostActivesRequest(by=MostActivesBy.VOLUME, top=100))
        symbols.update(a.symbol for a in actives.most_actives)

        movers = client.get_market_movers(MarketMoversRequest(market_type=MarketType.STOCKS, top=50))
        symbols.update(g.symbol for g in movers.gainers)
        symbols.update(l.symbol for l in movers.losers)

        logger.info(f"Alpaca screener contributed {len(symbols)} candidate symbols")
        return list(symbols)
    except Exception as e:
        logger.warning(f"Alpaca screener fetch failed ({e}), continuing without it")
        return []


def quality_filter_symbols(symbols: list) -> list:
    """Drops symbols that would otherwise pass the pure liquidity/
    volatility filter below but are noise for these strategies: warrants/
    rights/units (via Alpaca's own asset name), non-tradable or
    non-fractionable names, and bond/fixed-income ETFs (see
    EXCLUDED_NAME_KEYWORDS — these barely move, so they sit "near their
    high" almost constantly and mechanically trip breakout-style entries
    without the price action meaning anything for that asset class).
    Falls back to returning the input unfiltered if Alpaca credentials
    aren't available, rather than crashing the whole screener run.
    """
    kept, _ = quality_filter_symbols_with_names(symbols)
    return kept


def quality_filter_symbols_with_names(symbols: list):
    """Same as quality_filter_symbols, but also returns a {symbol: name}
    map (of the KEPT symbols) so callers can apply further name-based
    logic — e.g. rank_for_strategy's per-strategy leveraged/inverse
    exclusion — without a second round of per-symbol Alpaca lookups.
    """
    try:
        from alpaca.trading.client import TradingClient

        api_key = os.environ.get("ALPACA_API_KEY")
        api_secret = os.environ.get("ALPACA_API_SECRET")
        if not api_key or not api_secret:
            return symbols, {}

        client = TradingClient(api_key, api_secret, paper=True)
        junk_keywords = ("warrant", "right", "unit") + EXCLUDED_NAME_KEYWORDS
        kept = []
        names = {}
        for sym in symbols:
            try:
                asset = client.get_asset(sym)
            except Exception:
                # Not found via Alpaca (e.g. a symbol format mismatch) —
                # keep it rather than drop it; the liquidity/volatility
                # filter downstream will handle genuinely bad data.
                kept.append(sym)
                continue
            if not asset.tradable:
                continue
            name_lower = (asset.name or "").lower()
            if any(kw in name_lower for kw in junk_keywords):
                continue
            if not asset.fractionable:
                continue
            kept.append(sym)
            names[sym] = name_lower
        return kept, names
    except Exception as e:
        logger.warning(f"Quality filter failed ({e}), continuing unfiltered")
        return symbols, {}


def fetch_sp500_symbols() -> list:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; lumibot-screener/1.0)"}
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        tables = pd.read_html(pd.io.common.StringIO(resp.text))
        symbols = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        logger.info(f"Fetched {len(symbols)} S&P 500 symbols from Wikipedia")
        return symbols
    except Exception as e:
        logger.warning(f"S&P 500 fetch failed ({e}), using fallback sample of {len(FALLBACK_SP500_SAMPLE)}")
        return list(FALLBACK_SP500_SAMPLE)


def compute_liquidity_volatility(symbols: list) -> pd.DataFrame:
    """Avg dollar volume and annualized volatility per symbol over LOOKBACK_PERIOD."""
    rows = []
    chunk_size = 100
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        data = yf.download(
            chunk, period=LOOKBACK_PERIOD, group_by="ticker",
            progress=False, auto_adjust=True, threads=True,
        )
        for symbol in chunk:
            try:
                df = data[symbol] if len(chunk) > 1 else data
                closes = df["Close"].dropna()
                volumes = df["Volume"].dropna()
                if len(closes) < 20:
                    continue
                dollar_volume = float((closes * volumes).mean())
                daily_returns = closes.pct_change().dropna()
                annualized_vol = float(daily_returns.std() * (252 ** 0.5))
                rows.append({"symbol": symbol, "avg_dollar_volume": dollar_volume, "volatility": annualized_vol})
            except (KeyError, ValueError):
                continue
    return pd.DataFrame(rows)


def filter_candidates(market_df: pd.DataFrame) -> list:
    filtered = market_df[
        (market_df["avg_dollar_volume"] >= MIN_AVG_DOLLAR_VOLUME)
        & (market_df["volatility"] <= MAX_ANNUALIZED_VOLATILITY)
    ].sort_values("avg_dollar_volume", ascending=False)
    return filtered["symbol"].head(TOP_LIQUID_N).tolist()


def historical_quality(strategy_name: str) -> dict:
    """{symbol: win_rate} for symbols with enough labeled BUY history for
    this strategy, deduplicated against the sleeptime/backtest-loop
    duplication bug (see project memory)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            WITH deduped AS (
                SELECT symbol, outcome_label, MIN(id) as keep_id
                FROM signals
                WHERE strategy_name = ? AND action = 'BUY' AND outcome_label IS NOT NULL AND id > ?
                GROUP BY symbol, timestamp, confirmed, entry_price
            )
            SELECT symbol, COUNT(*) as n, AVG(outcome_label) as win_rate
            FROM deduped
            GROUP BY symbol
            HAVING n >= ?
            """,
            (strategy_name, CLEAN_DATA_WATERMARK, MIN_TESTED_SAMPLES),
        ).fetchall()
    finally:
        conn.close()
    return {row["symbol"]: row["win_rate"] for row in rows}


def rank_for_strategy(strategy_name: str, liquid_candidates: list, symbol_names: dict = None) -> list:
    symbol_names = symbol_names or {}
    if strategy_name in STRATEGIES_BLOCKING_LEVERAGED_INVERSE:
        before = len(liquid_candidates)
        liquid_candidates = [
            s for s in liquid_candidates
            if not any(kw in symbol_names.get(s, "") for kw in LEVERAGED_INVERSE_KEYWORDS)
        ]
        dropped = before - len(liquid_candidates)
        if dropped:
            logger.info(f"{strategy_name}: dropped {dropped} leveraged/inverse ETF candidate(s)")

    quality = historical_quality(strategy_name)
    tested = sorted(
        (s for s in liquid_candidates if s in quality),
        key=lambda s: quality[s], reverse=True,
    )
    untested = [s for s in liquid_candidates if s not in quality]
    ranked = tested + untested
    if not ranked:
        logger.warning(f"{strategy_name}: no liquid candidates matched — keeping hardcoded default")
        ranked = list(STRATEGY_DEFAULTS[strategy_name])
    else:
        ranked = ranked[:TOP_K_PER_STRATEGY]

    # Union in this strategy's protected symbols regardless of rank cutoff
    # — see PROTECTED_SYMBOLS.
    for protected in PROTECTED_SYMBOLS.get(strategy_name, ()):
        if protected not in ranked:
            ranked.append(protected)
    return ranked


def build_universes() -> dict:
    sp500 = fetch_sp500_symbols()
    alpaca_candidates = fetch_alpaca_screener_candidates()

    # Always include the strategies' existing hardcoded symbols so their
    # accumulated signal history stays eligible for re-ranking even if one
    # ever fell out of the S&P 500 list itself.
    known_symbols = set(sp500) | set(alpaca_candidates)
    for defaults in STRATEGY_DEFAULTS.values():
        known_symbols.update(defaults)

    filtered_symbols, symbol_names = quality_filter_symbols_with_names(sorted(known_symbols))
    known_symbols = set(filtered_symbols)
    logger.info(
        f"{len(known_symbols)} symbols after warrant/tradability/bond-ETF/"
        f"money-market/alt-strategy quality filter"
    )

    market_df = compute_liquidity_volatility(sorted(known_symbols))
    liquid_candidates = filter_candidates(market_df)
    logger.info(f"{len(liquid_candidates)} symbols passed the liquidity/volatility filter")

    universes = {}
    for strategy_name in STRATEGY_DEFAULTS:
        universes[strategy_name] = rank_for_strategy(strategy_name, liquid_candidates, symbol_names)
        logger.info(f"{strategy_name}: {universes[strategy_name]}")

    return universes


def main():
    universes = build_universes()
    with open("symbol_universes.json", "w") as f:
        json.dump(universes, f, indent=2)
    logger.info("Wrote symbol_universes.json")


if __name__ == "__main__":
    main()
