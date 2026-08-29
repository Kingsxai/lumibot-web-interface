import logging
logger = logging.getLogger(__name__)

class ReversalStrategy:
    def __init__(self, strategy):
        self.strategy = strategy

    def analyze(self):
        """Catch turning points after extended moves."""
        decisions = {}
        for symbol in ["AAPL", "MSFT", "QQQ"]:
            bars = self.strategy.get_historical_prices(symbol, 10, "day")
            if not bars or len(bars.df) < 10:
                continue
            df = bars.df
            recent = df["close"].iloc[-5:]
            if recent.is_monotonic_increasing:  # extended uptrend
                decisions[symbol] = "SELL"
            elif recent.is_monotonic_decreasing:  # extended downtrend
                decisions[symbol] = "BUY"
        return decisions
