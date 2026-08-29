import logging
logger = logging.getLogger(__name__)

class GapAndGoStrategy:
    def __init__(self, strategy):
        self.strategy = strategy

    def analyze(self):
        """Trade stocks that gap at open."""
        decisions = {}
        for symbol in ["AAPL", "TSLA", "NVDA"]:
            bars = self.strategy.get_historical_prices(symbol, 2, "day")
            if not bars or len(bars.df) < 2:
                continue
            prev_close = bars.df["close"].iloc[-2]
            today_open = bars.df["open"].iloc[-1]
            gap = (today_open - prev_close) / prev_close
            if gap > 0.03:  # gap up
                decisions[symbol] = "BUY"
            elif gap < -0.03:  # gap down
                decisions[symbol] = "SELL"
        return decisions
