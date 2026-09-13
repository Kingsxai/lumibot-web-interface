import logging

from symbol_universe import load_universe

logger = logging.getLogger(__name__)

# Built 2026-09-08 after confirming on real 1-minute data (NVDA, live Alpaca
# bars) that MACD histogram sign-flips and Stoch RSI K/D crosses sync ~91%
# of the time within +/-3 minutes of each other — same relationship first
# spotted on a GBP/INR TradingView chart the user shared, then validated on
# a symbol this account can actually trade. This strategy requires BOTH
# oscillators to agree before entering, instead of scalping.py's single
# 0.2% 1-minute price-move trigger.
#
# Independent from scalping.py — its own universe, its own strategy_name
# ("scalping_v2"), entirely separate entry logic. Not a replacement; runs
# alongside it (own dashboard toggle, seeded OFF — see signal_logger.py)
# so real results can be compared strategy vs strategy in signals.db, same
# as any other strategy addition in this project.
#
# Classified "self_gated" in confirmation.py's STRATEGY_TYPES, not "trend"
# — the entry condition below already IS a secondary-indicator confirmation
# (Stoch RSI confirming MACD), and confirmation.py's daily-bar 50-day-SMA
# trend check is miscalibrated for a minute-bar strategy (confirmed
# elsewhere: scalping.py's real confirm rate under that gate is ~2.2% vs
# ~10-20% for the daily-bar strategies it was tuned for) — stacking that
# gate on top here would just reproduce the same near-zero-fire problem for
# a different reason.
#
# Same backtesting exclusion as scalping.py/market_profile.py, same reason:
# Lumibot's YahooDataBacktesting cache is keyed by asset only, not (asset,
# timestep), so a "minute" request during a backtest silently returns
# cached DAILY data and fabricates signals. Never validate this strategy
# through backtest_runner.py/run_multi_backtest.py — real minute data only
# (Alpaca's historical API directly).

# Enough recent 1-minute bars for MACD's slow EMA (26) + signal EMA (9) and
# Stoch RSI's RSI length (14) + stoch length (14) + K/D smoothing (3/3) to
# all be fully warmed up — 60 gives real headroom above the ~40-bar floor
# either indicator strictly needs.
LOOKBACK_MINUTES = 60

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
STOCH_RSI_LEN, STOCH_LEN, STOCH_K, STOCH_D = 14, 14, 3, 3

# A MACD flip and a Stoch RSI cross must both have happened within this many
# of the most recent bars to count as "current" confluence — without this, a
# MACD flip from 40 minutes ago would still "confirm" against today's Stoch
# RSI cross, which isn't the same real-time relationship the +/-3min live
# validation actually found.
RECENCY_WINDOW_MINUTES = 3


def _macd_histogram(close):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return macd_line - signal_line


def _stoch_rsi(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(STOCH_RSI_LEN).mean()
    loss = (-delta.clip(upper=0)).rolling(STOCH_RSI_LEN).mean()
    rsi = 100 - (100 / (1 + gain / loss))
    min_rsi = rsi.rolling(STOCH_LEN).min()
    max_rsi = rsi.rolling(STOCH_LEN).max()
    stoch = (rsi - min_rsi) / (max_rsi - min_rsi) * 100
    k_line = stoch.rolling(STOCH_K).mean()
    d_line = k_line.rolling(STOCH_D).mean()
    return k_line, d_line


def _crossed_up(series):
    """True if `series` (a short recent window) crossed from <=0 to >0 at
    any point — used for both the MACD histogram's own sign and the Stoch
    RSI K-minus-D difference."""
    return bool(((series.shift(1) <= 0) & (series > 0)).any())


def _crossed_down(series):
    return bool(((series.shift(1) >= 0) & (series < 0)).any())


class ScalpingV2Strategy:
    def __init__(self, strategy):
        self.strategy = strategy
        self.symbols = load_universe("scalping_v2", ["AAPL", "MSFT", "AMZN", "NVDA", "AMD"])
        self.signal_reasons = {}

    def get_signal_reason(self, symbol):
        return self.signal_reasons.get(symbol)

    def analyze(self):
        """MACD histogram bullish flip + Stoch RSI K/D bullish cross, both
        within the last RECENCY_WINDOW_MINUTES bars, on 1-minute data."""
        decisions = {}
        self.signal_reasons = {}
        # See module docstring — never meaningfully testable through
        # Lumibot's own backtest engine.
        if getattr(self.strategy, "is_backtesting", False):
            return decisions

        for symbol in self.symbols:
            bars = self.strategy.get_historical_prices(symbol, LOOKBACK_MINUTES, "minute")
            if not bars or len(bars.df) < LOOKBACK_MINUTES:
                continue
            close = bars.df["close"]

            hist = _macd_histogram(close)
            k_line, d_line = _stoch_rsi(close)
            if hist.isna().iloc[-1] or k_line.isna().iloc[-1] or d_line.isna().iloc[-1]:
                continue

            window = slice(-(RECENCY_WINDOW_MINUTES + 1), None)
            recent_hist = hist.iloc[window]
            recent_kd_diff = (k_line - d_line).iloc[window]

            macd_bull = _crossed_up(recent_hist)
            macd_bear = _crossed_down(recent_hist)
            stoch_bull = _crossed_up(recent_kd_diff)
            stoch_bear = _crossed_down(recent_kd_diff)

            if macd_bull and stoch_bull:
                decisions[symbol] = "BUY"
                self.signal_reasons[symbol] = (
                    f"MACD/Stoch RSI bullish confluence "
                    f"(hist {hist.iloc[-1]:+.4f}, K {k_line.iloc[-1]:.1f}/D {d_line.iloc[-1]:.1f})"
                )
            elif macd_bear or stoch_bear:
                decisions[symbol] = "SELL"
                self.signal_reasons[symbol] = "MACD/Stoch RSI bearish flip"
        return decisions
