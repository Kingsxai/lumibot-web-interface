# Lumibot Multi-Strategy Trading Bot with Web Interface

A production-ready web interface for running multiple Lumibot strategies simultaneously with:
- **Momentum Allocator**: Dynamically allocates capital to top-performing momentum stocks
- **News Sentiment Analysis**: Analyzes market sentiment to inform trading decisions
- **Bracket Orders**: Automatic stop-loss and take-profit levels
- **Graceful Shutdown**: Liquidates all positions safely when stopping
- **Web Dashboard**: Real-time monitoring and control

## Features

✅ Run multiple strategies in parallel
✅ Multi-stock position management
✅ Bracket order (stop-loss + take-profit) support
✅ News sentiment integration
✅ REST API for external control
✅ WebSocket for real-time updates
✅ Graceful shutdown with position liquidation
✅ Portfolio analytics and performance tracking
✅ Order history and trade logging

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set Environment Variables

```bash
export ALPACA_API_KEY='your-alpaca-key'
export ALPACA_API_SECRET='your-alpaca-secret'
export ALPACA_IS_PAPER=true
export NEWSAPI_KEY='your-newsapi-key'  # Optional: for news sentiment
export FLASK_ENV=production
```

### 3. Run the Bot

```bash
python bot_runner.py
```

The web dashboard will be available at `http://localhost:5000`

## Architecture

```
┌─────────────────────────────────────────────────────┐
│         Web Dashboard (Vue.js)                      │
│  Real-time monitoring, order placement, controls    │
└──────────────────┬──────────────────────────────────┘
                   │ HTTP + WebSocket
┌──────────────────▼──────────────────────────────────┐
│         Flask API Server (api.py)                   │
│  REST endpoints, WebSocket, strategy coordination   │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│    Multi-Strategy Manager (strategy_manager.py)     │
│  • Momentum Allocator Strategy                      │
│  • News Sentiment Strategy                          │
│  • Order Management & Bracket Orders                │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│         Lumibot Core (Alpaca Broker)                │
│  Live/Paper Trading, Order Execution                │
└──────────────────────────────────────────────────────┘
```

## Configuration

Edit `config.py` to customize:
- Portfolio allocation percentages
- Risk parameters (max position size, stop-loss %)
- Momentum lookback periods
- News sentiment weights
- Stock universe

## API Endpoints

### GET /api/status
Get bot status, portfolio value, and positions

### GET /api/positions
List all open positions

### GET /api/orders
List all orders (open and closed)

### POST /api/orders
Place a new order
```json
{"symbol": "AAPL", "quantity": 10, "side": "buy"}
```

### POST /api/start
Start trading

### POST /api/stop
Stop trading and liquidate positions

### GET /api/strategies
Get active strategies and their performance

## Web Dashboard

Access at `http://localhost:5000`

Features:
- Portfolio overview (value, cash, buying power)
- Real-time positions table
- Active orders and trade history
- Strategy performance metrics
- Manual order placement
- Start/Stop controls
- P&L tracking

## Deployment

### Local VM
```bash
python bot_runner.py
```

### Docker
```bash
docker build -t lumibot-trader .
docker run -p 5000:5000 -e ALPACA_API_KEY=xxx lumibot-trader
```

### Cloud (AWS/GCP/Azure)
See `deployment/` directory for cloud setup guides

## Safety & Disclaimer

⚠️ **Paper Trading by Default** - Set `ALPACA_IS_PAPER=false` only after thorough testing
⚠️ **Risk Management** - Always set stop-loss levels and position size limits
⚠️ **Testing** - Backtest strategies before live trading
⚠️ **Monitoring** - Monitor bot performance regularly

This software is for educational purposes. Always understand the risks before live trading.

## License

MIT
