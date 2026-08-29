"""Main entry point for the Lumibot Multi-Strategy Trading Bot.

This script initializes the trading bot and starts the Flask web interface.
"""

import os
import logging
import threading
from datetime import datetime

from dotenv import load_dotenv
from flask_socketio import SocketIO

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Import after logging configuration
import config
from strategy_manager import MultiStrategyBot
from lumibot.brokers import Alpaca
from api import app, socketio, init_bot


def initialize_broker():
    """Initialize Alpaca broker connection."""
    logger.info("Initializing Alpaca broker...")
    
    # Get API credentials from environment
    api_key = os.environ.get('ALPACA_API_KEY')
    api_secret = os.environ.get('ALPACA_API_SECRET')
    is_paper = os.environ.get('ALPACA_IS_PAPER', 'true').lower() == 'true'
    
    if not api_key or not api_secret:
        raise ValueError(
            "ALPACA_API_KEY and ALPACA_API_SECRET environment variables are required"
        )
    
    # Create broker instance with dictionary config (correct way for Lumibot)
    broker = Alpaca({
        "API_KEY": api_key,
        "API_SECRET": api_secret,
        "PAPER": is_paper
    })
    
    logger.info(f"Broker initialized (Paper Trading: {is_paper})")
    return broker


def initialize_strategy_bot(broker):
    """Initialize the multi-strategy trading bot."""
    logger.info("Initializing MultiStrategyBot...")
    
    # Create strategy instance
    strategy = MultiStrategyBot(broker=broker)
    
    logger.info("Strategy bot initialized successfully")
    return strategy


def run_bot_in_background(strategy_bot):
    """Run the bot's main loop in a background thread."""
    def bot_loop():
        try:
            logger.info("Starting bot trading loop...")
            strategy_bot.run(parameters={})
        except Exception as e:
            logger.error(f"Error in bot loop: {e}", exc_info=True)
    
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("Bot loop started in background thread")
    return bot_thread


def broadcast_initial_status(strategy_bot):
    """Send initial status to all connected clients."""
    try:
        from datetime import datetime
        data = {
            "timestamp": datetime.now().isoformat(),
            "status": "initialized",
            "portfolio_value": float(strategy_bot.get_portfolio_value()),
            "cash": float(strategy_bot.get_cash()),
            "positions": [],
            "open_orders": 0,
            "bracket_orders": 0,
        }
        socketio.emit("bot_status", data, broadcast=True)
    except Exception as e:
        logger.warning(f"Could not broadcast initial status: {e}")


def main():
    """Main entry point for the application."""
    logger.info("=" * 80)
    logger.info("Lumibot Multi-Strategy Trading Bot with Web Interface")
    logger.info(f"Started at {datetime.now()}")
    logger.info("=" * 80)
    
    try:
        # Initialize broker
        broker = initialize_broker()
        
        # Initialize strategy bot
        strategy_bot = initialize_strategy_bot(broker)
        
        # Initialize API with strategy bot instance
        init_bot(strategy_bot)
        
        # Start bot in background thread
        bot_thread = run_bot_in_background(strategy_bot)
        
        # Broadcast initial status
        broadcast_initial_status(strategy_bot)
        
        # Get host and port from environment or use defaults
        host = os.getenv('FLASK_HOST', '127.0.0.1')
        port = int(os.getenv('FLASK_PORT', 5000))
        debug = os.getenv('FLASK_ENV', 'production') == 'development'
        
        logger.info(f"Starting Flask web server on {host}:{port}")
        logger.info(f"Dashboard available at http://{host}:{port}")
        logger.info("Press Ctrl+C to stop the bot")
        
        # Start Flask app with SocketIO
        socketio.run(
            app,
            host=host,
            port=port,
            debug=debug,
            allow_unsafe_werkzeug=True
        )
        
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down gracefully...")
        strategy_bot.stop_trading()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
