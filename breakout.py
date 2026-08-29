import logging
logger = logging.getLogger(__name__)

class BreakoutStrategy:
    def __init__(self, strategy):
        self.strategy = strategy

    def analyze(self):
        """Detect breakout above resistance or below support."""
        decisions = {}
        for symbol in ["AAPL", "MSFT", "TSLA"]:
            bars = self.strategy.get_historical_prices(symbol, 20, "day")
            if not bars or len(bars.df) < 20:
                continue
            high = bars.df["high"].max()
            low = bars.df["low"].min()
            current = bars.df["close"].iloc[-1]
            if current > high * 0.99:  # breakout up
                decisions[symbol] = "BUY"
            elif current < low * 1.01:  # breakdown
                decisions[symbol] = "SELL"
        return decisions
