import os
from decimal import Decimal

# ============================================================================
# BROKER CONFIGURATION
# ============================================================================

BROKER_CONFIG = {
    "API_KEY": os.environ.get("ALPACA_API_KEY"),
    "API_SECRET": os.environ.get("ALPACA_API_SECRET"),
    "PAPER": os.environ.get("ALPACA_IS_PAPER", "true").lower() == "true",
}

# ============================================================================
# PORTFOLIO & RISK MANAGEMENT
# ============================================================================

# Maximum % of portfolio to allocate per position
MAX_POSITION_SIZE = 0.10  # 10% per position

# Stop loss % from entry price
STOP_LOSS_PERCENT = 0.05  # 5%

# Take profit % from entry price
TAKE_PROFIT_PERCENT = 0.10  # 10%

# Maximum number of concurrent positions
MAX_POSITIONS = 10

# Minimum cash buffer (don't invest 100% of portfolio)
MIN_CASH_BUFFER = 0.05  # Keep 5% cash

# ============================================================================
# MOMENTUM ALLOCATOR STRATEGY
# ============================================================================

# Momentum lookback period (days)
MOMENTUM_LOOKBACK_DAYS = 20

# Number of top momentum stocks to track
MOMENTUM_TOP_N = 5

# Rebalance every N minutes
MOMENTUM_REBALANCE_INTERVAL = 60

# Allocation % to momentum strategy
MOMENTUM_ALLOCATION_PERCENT = 0.60  # 60% of capital

# Stock universe for momentum (customize as needed)
MOMENTUM_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "TSLA", "META", "NFLX", "ADBE", "CRM",
    "PYPL", "ZOOM", "INTU", "SNPS", "CDNS",
    "AMD", "MU", "QCOM", "MCHP", "AVGO"
]

# ============================================================================
# NEWS SENTIMENT STRATEGY
# ============================================================================

# NewsAPI key (get from https://newsapi.org)
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

# Sentiment score thresholds
SENTIMENT_BUY_THRESHOLD = 0.6  # Buy if sentiment > 0.6
SENTIMENT_SELL_THRESHOLD = 0.3  # Sell if sentiment < 0.3

# News lookback window (hours)
NEWS_LOOKBACK_HOURS = 24

# Allocation % to news sentiment strategy
NEWS_ALLOCATION_PERCENT = 0.40  # 40% of capital

# Stock universe for news sentiment
NEWS_SENTIMENT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "TSLA", "META", "SPY", "QQQ", "IWM"
]

# ============================================================================
# TRADING HOURS
# ============================================================================

# Market hours (EST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

# Pre-market trading
ENABLE_PREMARKET = False

# After-hours trading
ENABLE_AFTERHOURS = False

# ============================================================================
# API & WEB SERVER
# ============================================================================

FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))
FLASK_DEBUG = os.environ.get("FLASK_ENV", "production") == "development"

# ============================================================================
# LOGGING & MONITORING
# ============================================================================

LOG_LEVEL = "INFO"
LOG_FILE = "bot.log"
KEEP_TRADE_HISTORY = True
TRADE_HISTORY_FILE = "trades.json"

# ============================================================================
# ADVANCED OPTIONS
# ============================================================================

# Enable live data updates (slower but real-time)
ENABLE_LIVE_DATA = True

# Cache price data locally (reduces API calls)
ENABLE_PRICE_CACHE = True
PRICE_CACHE_TTL_SECONDS = 300  # 5 minutes

# Graceful shutdown timeout (seconds)
GRACEFUL_SHUTDOWN_TIMEOUT = 30
