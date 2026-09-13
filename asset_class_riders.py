"""Optional asset-class-specific "second opinion" riders (2026-09-05).

Each rider is an independent, out-of-band macro/cross-asset signal — not
derived from the traded symbol's own price/volume — that a strategy can
optionally consult before firing a BUY. Same non-compulsory add-on pattern
as news_sentiment.py's rider for scalping: these never gate an exit, and
they never hard-fail live trading. Any data-fetch problem fails OPEN
(treated as "no opinion", confirmed=True) so an auxiliary yfinance/FRED
hiccup can never stall real order flow — the same fail-open philosophy
confirmation.py and meta_model.get_confidence() already use.

Backed by real, named intermarket mechanisms (2026-09-05 research), not
just historical correlation:
  - VIX: the equity market's own priced-in fear gauge — elevated/rising
    VIX is a real risk-off signal for the 8 live strategies, all of which
    currently trade individual US equities only (symbol_universe.py's
    KID_RESTRICTED_SYMBOLS excludes every ETF/commodity ticker from them).
  - Gold macro (DXY + real yields): gold is dollar-denominated (inverse to
    DXY) and competes with yield-bearing assets (inverse to real 10Y TIPS
    yield) — two direct causal mechanisms, not a loose correlation.
    Applied to the whole COMMODITY pod as a reasonable approximation;
    tuned specifically for gold, the pod's dominant instrument.
  - Crypto rotation (ETH/BTC ratio): capital rotates BTC -> ETH -> alts in
    a fairly consistent order; a rising ratio confirms risk appetite is
    expanding into an altcoin buy, a falling one means capital is
    defensively parking in BTC.

No real forex carry rider yet: a genuine interest-rate-differential check
needs each currency's live central-bank policy rate (FRED has these, one
series per currency/bank), not wired into this project yet.
check_forex_carry_rider is a stub that always passes and says so in its
features — do not treat it as implemented.
"""

import logging

import config

logger = logging.getLogger(__name__)

_YF_UNAVAILABLE_LOGGED = False


def _fetch_yf_close(ticker: str, period: str = "3mo"):
    """Best-effort yfinance daily-close fetch. Returns a pandas Series
    (oldest -> newest) or None on any failure — callers must fail open."""
    global _YF_UNAVAILABLE_LOGGED
    try:
        import yfinance as yf
    except ImportError:
        if not _YF_UNAVAILABLE_LOGGED:
            logger.warning("yfinance not installed — asset-class riders disabled")
            _YF_UNAVAILABLE_LOGGED = True
        return None

    try:
        df = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty or "Close" not in df:
            return None
        close = df["Close"]
        # yfinance sometimes returns a 1-column DataFrame for a single
        # ticker instead of a Series depending on version — flatten it.
        if hasattr(close, "iloc") and close.ndim == 2:
            close = close.iloc[:, 0]
        return close.dropna()
    except Exception as e:
        logger.warning(f"Rider data fetch failed for {ticker}: {e}")
        return None


def check_vix_rider() -> tuple:
    """Risk-on/risk-off gate for equity BUYs, using CBOE's VIX.

    Confirmed (pass) when VIX is below config.VIX_HIGH_THRESHOLD AND hasn't
    spiked more than config.VIX_SPIKE_MAX_INCREASE_PCT over the last
    config.VIX_SPIKE_LOOKBACK_DAYS trading days. Fails open (confirmed=True)
    on any data problem — this is a second opinion, not a hard requirement.
    """
    if not getattr(config, "ENABLE_VIX_RIDER", False):
        return True, {"vix_rider": "disabled"}

    close = _fetch_yf_close("^VIX")
    lookback = config.VIX_SPIKE_LOOKBACK_DAYS
    if close is None or len(close) <= lookback:
        return True, {"vix_rider": "no_data"}

    current_vix = float(close.iloc[-1])
    prior_vix = float(close.iloc[-1 - lookback])
    spike_pct = ((current_vix - prior_vix) / prior_vix) if prior_vix else 0.0

    below_threshold = current_vix < config.VIX_HIGH_THRESHOLD
    not_spiking = spike_pct <= config.VIX_SPIKE_MAX_INCREASE_PCT
    confirmed = below_threshold and not_spiking

    return confirmed, {
        "vix_rider": "checked",
        "vix_current": round(current_vix, 2),
        "vix_spike_pct": round(spike_pct, 4),
        "vix_below_threshold": below_threshold,
        "vix_not_spiking": not_spiking,
    }


def check_gold_macro_rider() -> tuple:
    """DXY + real-yields gate for the COMMODITY pod (gold-tuned).

    Confirmed (pass) when the US Dollar Index isn't breaking to a new
    short-term high AND (when real-yield data is available) the 10-year
    real yield isn't sharply rising — both are genuine headwinds for a
    dollar-denominated, non-yielding asset like gold. Real-yield data
    (FRED's DFII10) isn't wired into this project's data pipeline yet, so
    that half of the check is skipped (not faked) when unavailable — the
    DXY half alone still runs.
    """
    if not getattr(config, "ENABLE_GOLD_MACRO_RIDER", False):
        return True, {"gold_macro_rider": "disabled"}

    dxy = _fetch_yf_close("DX-Y.NYB")
    lookback = config.GOLD_MACRO_DXY_LOOKBACK_DAYS
    if dxy is None or len(dxy) <= lookback:
        return True, {"gold_macro_rider": "no_data"}

    current_dxy = float(dxy.iloc[-1])
    recent_high = float(dxy.iloc[-lookback:].max())
    # "Breaking higher" = making a new high within the lookback window,
    # not just being above some fixed level.
    dxy_breaking_higher = current_dxy >= recent_high

    confirmed = not dxy_breaking_higher
    return confirmed, {
        "gold_macro_rider": "checked_dxy_only",
        "dxy_current": round(current_dxy, 2),
        "dxy_recent_high": round(recent_high, 2),
        "dxy_breaking_higher": dxy_breaking_higher,
        "real_yields_checked": False,
    }


def check_crypto_rotation_rider(symbol: str) -> tuple:
    """Capital-rotation gate for the CRYPTO pod, via the ETH/BTC ratio.

    BTC itself has no rider opinion (the rotation thesis is about capital
    leaving/entering BTC for alts, not about BTC's own entries). For any
    other coin, confirmed (pass) when the ETH/BTC ratio is rising over
    config.CRYPTO_ROTATION_LOOKBACK_DAYS — i.e. capital is rotating out of
    BTC into higher-beta coins, the real precondition for an altcoin rally.
    """
    if not getattr(config, "ENABLE_CRYPTO_ROTATION_RIDER", False):
        return True, {"crypto_rotation_rider": "disabled"}

    if symbol.upper() in ("BTC", "BTCUSD", "BTC-USD"):
        return True, {"crypto_rotation_rider": "not_applicable_btc"}

    eth = _fetch_yf_close("ETH-USD")
    btc = _fetch_yf_close("BTC-USD")
    lookback = config.CRYPTO_ROTATION_LOOKBACK_DAYS
    if eth is None or btc is None:
        return True, {"crypto_rotation_rider": "no_data"}

    ratio = (eth / btc).dropna()
    if len(ratio) <= lookback:
        return True, {"crypto_rotation_rider": "no_data"}

    current_ratio = float(ratio.iloc[-1])
    prior_ratio = float(ratio.iloc[-1 - lookback])
    ratio_rising = current_ratio > prior_ratio

    return ratio_rising, {
        "crypto_rotation_rider": "checked",
        "eth_btc_ratio_current": round(current_ratio, 6),
        "eth_btc_ratio_prior": round(prior_ratio, 6),
        "ratio_rising": ratio_rising,
    }


def check_forex_carry_rider(symbol: str) -> tuple:
    """STUB — not a real check yet.

    A genuine interest-rate-differential confirmation needs each currency's
    live central-bank policy rate (FRED has these, but per-currency and not
    wired into this project's data pipeline). Always passes and reports
    itself as not implemented so it's never mistaken for a working gate —
    do not enable ENABLE_FOREX_CARRY_RIDER expecting real behavior.
    """
    return True, {"forex_carry_rider": "not_implemented"}
