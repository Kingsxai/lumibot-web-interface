"""Main entry point for the Lumibot Multi-Strategy Trading Bot.

This script initializes the trading bot and starts the Flask web interface.
"""

import os
import logging
import threading
import time
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
from lumibot.brokers import Alpaca, InteractiveBrokers
from lumibot.traders import Trader
from api import app, socketio, init_bot, init_extended_hours_trader, init_international_markets_trader
from signal_logger import set_setting

# 2026-09-02: this account has no real-time market-data subscription for
# at least SPY and MU (confirmed live — IB error codes 10089/10168), so
# every get_last_price()/get_tick() call for such a symbol stalls
# ~13s before giving up. initialize_broker() below requests delayed
# (type 3) data as a fallback — but Lumibot's own IBWrapper.tickPrice
# only recognizes LIVE tick types (1=bid, 2=ask, 4=last, 9=close), not
# their delayed equivalents (66/67/68/75) that IB sends once a
# connection is on delayed data. Without this patch, delayed ticks would
# arrive and be silently dropped — reqMarketDataType(3) alone doesn't
# actually fix anything. Patching the installed library's method at
# runtime (not editing site-packages, which a venv reinstall would wipe)
# — just remaps the delayed codes onto their live equivalents before
# calling the original, so every existing caller keeps working unchanged.
from lumibot.brokers.interactive_brokers import IBWrapper as _IBWrapper
_original_tick_price = _IBWrapper.tickPrice
_DELAYED_TICK_MAP = {66: 1, 67: 2, 68: 4, 75: 9}


def _delayed_aware_tick_price(self, reqId, tickType, price, attrib):
    # 2026-09-03: this patch only ever remapped the tick TYPE code, never
    # validated the price itself. IB uses -1 (and sometimes 0) as a "no
    # data available for this field right now" sentinel on delayed tick
    # types, same convention as its bidSize/askSize fields — confirmed
    # live: get_last_price() started returning a literal -1.0, which
    # then flowed into _place_bracket_order's available_capital/
    # current_price sizing math as a negative "price", quietly
    # cancelling every entry with "Position size is 0" (silent, no
    # crash, just no trade — exactly why real orders kept getting
    # rejected right after IB Gateway restarts, when data farm
    # connections haven't fully settled and -1 sentinels are common).
    # Drop the tick entirely when it's non-positive rather than forward
    # a fabricated negative price — the original method already
    # tolerates a missing tick fine (that's the whole point of the
    # delayed-data fallback this patch exists for in the first place).
    if price is not None and price <= 0:
        return
    _original_tick_price(self, reqId, _DELAYED_TICK_MAP.get(tickType, tickType), price, attrib)


_IBWrapper.tickPrice = _delayed_aware_tick_price

# 2026-09-02: Flask serves several dashboard endpoints concurrently
# (/api/status, /api/system-health, broadcast_update, etc.), often within
# the same ~1s poll burst, and each independently calls into
# strategy_bot.get_cash()/get_buying_power()/get_portfolio_value() —
# which all route through InteractiveBrokers._get_balances_at_broker() ->
# reqAccountSummary. That method is a blocking request/wait/cancel round
# trip with no caching or locking, so a poll burst fires several
# CONCURRENT reqAccountSummary subscriptions on the one IB connection.
# IB Gateway only tolerates one outstanding subscription and starts
# rejecting the rest (confirmed live 2026-09-02 — IB error 322, "Maximum
# number of account summary requests exceeded"), whose queue then times
# out and returns None, which crashes float(None) in api.py and adds
# contention to the same IB connection the real trading iteration also
# needs. Fix: serialize calls with a lock and cache the result for a
# short TTL, so a poll burst makes one real IB round trip instead of N
# concurrent ones — same spirit as api.py's broadcast_update() 5s
# throttle fix, applied one level lower since every dashboard endpoint
# (not just broadcast_update) independently triggers this call.
from lumibot.brokers.interactive_brokers import InteractiveBrokers as _IBBroker
_original_get_balances_at_broker = _IBBroker._get_balances_at_broker
_balances_lock = threading.Lock()
_balances_cache = {"value": None, "at": 0.0}
_BALANCES_CACHE_TTL_SECONDS = 3.0


def _cached_get_balances_at_broker(self, quote_asset, strategy):
    with _balances_lock:
        now = time.monotonic()
        if _balances_cache["value"] is not None and (now - _balances_cache["at"]) < _BALANCES_CACHE_TTL_SECONDS:
            return _balances_cache["value"]
        result = _original_get_balances_at_broker(self, quote_asset, strategy)
        if result is not None:
            _balances_cache["value"] = result
            _balances_cache["at"] = now
        return result


_IBBroker._get_balances_at_broker = _cached_get_balances_at_broker


def initialize_broker():
    """Initialize the broker connection. BROKER_PROVIDER env var selects
    which one (2026-09-02) — defaults to alpaca, set to
    interactive_brokers to trade through the local IB Gateway
    (see /home/VMbot01/ibkr/start_ib_gateway.sh, must already be
    running and logged in)."""
    provider = os.environ.get('BROKER_PROVIDER', 'alpaca').lower()

    if provider == 'interactive_brokers':
        logger.info("Initializing Interactive Brokers broker...")
        ip = os.environ.get('INTERACTIVE_BROKERS_IP', '127.0.0.1')
        port = os.environ.get('INTERACTIVE_BROKERS_PORT')
        client_id = os.environ.get('INTERACTIVE_BROKERS_CLIENT_ID')

        if not port or not client_id:
            raise ValueError(
                "INTERACTIVE_BROKERS_PORT and INTERACTIVE_BROKERS_CLIENT_ID "
                "environment variables are required"
            )

        broker = InteractiveBrokers({
            "IP": ip,
            "SOCKET_PORT": int(port),
            "CLIENT_ID": int(client_id),
            "IB_SUBACCOUNT": os.environ.get('IB_SUBACCOUNT'),
        })

        # 2026-09-02: this account has no real-time market-data
        # subscription for at least SPY and MU (confirmed live — IB error
        # codes 10089/10168, "requires additional subscription... Delayed
        # market data is available" / "not enabled"). Lumibot's own
        # InteractiveBrokers broker never calls IB's reqMarketDataType,
        # so it always requests LIVE (type 1) and simply times out
        # (~13s/symbol) rather than falling back — even though free
        # delayed data (type 3) is available for exactly this case.
        # reqMarketDataType is a per-connection client setting (not
        # per-request): IB serves delayed data for anything not
        # entitled to live, and live data unaffected for anything that
        # IS entitled. One call, no downside, fixes get_last_price()
        # stalling for scalping and anywhere else it's called.
        broker.ib.reqMarketDataType(3)
        logger.info("Requested delayed (type 3) market data fallback for symbols without a real-time subscription")

        # 2026-09-03: reqMarketDataType(3) above doesn't eliminate the
        # 13s stall for every symbol — plenty of names (SPCX, MSFT,
        # INTU, AAPL, NVDA, GME, SVT, LMP, confirmed live) still just
        # never get a tick back at all in delayed mode, and Lumibot's
        # IBClient.get_tick() (interactive_brokers.py ~line 1119) blocks
        # on `tick_storage.get(timeout=self.max_wait_time)` — a queue
        # that's empty either way, live or delayed, if IB never sends
        # anything for that reqId. get_positions() uses the exact same
        # attribute for its own blocking queue.get() (~line 1282), so
        # every position refresh pays the same cost too.
        #
        # self.max_wait_time is a single hardcoded `= 13` in IBClient.
        # __init__ (venv/lib/python3.12/site-packages/lumibot/brokers/
        # interactive_brokers.py:1094) shared by EVERY blocking lookup
        # on this connection (get_tick, get_historical_data,
        # get_positions, get_account_summary, get_orders,
        # get_contract_details, get_option_params) — none of it governs
        # order submission/fill confirmation, which is handled entirely
        # separately (async orderStatus callbacks / this project's own
        # ib_aux.wait_for), so shortening it only affects how long a
        # doomed lookup gets to fail, not order execution.
        #
        # Since set_market('24/5') started running on_trading_iteration
        # continuously today (2026-09-03), a single pre-market cycle
        # racked up 6+ failed get_tick calls plus 3 failed
        # get_positions calls in a row — 13 x 13s = 169s of pure
        # timeout, pushing real iterations to 87-120s and climbing
        # toward watchdog.sh's 180s STALL_THRESHOLD_SECONDS. A lookup
        # that's going to fail does so the same way whether given 13s
        # or 5s (falls back to yesterday's close either way) — cutting
        # the ceiling to 5s doesn't change any trading decision, it
        # just fails faster.
        broker.ib.max_wait_time = 5
        logger.info("Shortened IB blocking-lookup timeout from 13s to 5s (get_tick/get_positions/etc. — see comment)")

        logger.info(f"Broker initialized (Interactive Brokers, {ip}:{port})")
        return broker

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
    """Run the bot's main loop in a background thread via Lumibot's Trader."""
    def bot_loop():
        try:
            logger.info("Starting bot trading loop...")
            trader = Trader(logfile="", backtest=False)
            trader.add_strategy(strategy_bot)
            trader.run_all(show_plot=False, show_tearsheet=False, save_tearsheet=False)
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
        socketio.emit("bot_status", data)
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

        # 2026-09-03: extended_hours_trading_enabled/international_
        # markets_trading_enabled toggles removed (see ib_side_channel_
        # trader.py's module docstring) — always on whenever the bot
        # itself is trading, gated by is_trading alone now.
        # Same manual-gate principle — never start a fresh process
        # silently paused from a setting left over in the DB (see
        # signal_logger.py's "new_entries_paused" default for the full
        # reasoning).
        set_setting("new_entries_paused", 0.0)

        # Start bot in background thread
        bot_thread = run_bot_in_background(strategy_bot)

        # 2026-09-03 consolidation ("only one bot handling everything") —
        # strategy_bot.ib_aux (built in MultiStrategyBot.initialize(), is
        # None for a non-IB broker) is now driven synchronously from
        # strategy_bot's own on_trading_iteration loop instead of a
        # separate background thread with its own schedule/connection
        # lifecycle. api.py's init_extended_hours_trader/init_
        # international_markets_trader still point at the SAME object —
        # only how it's driven changed, not the dashboard-facing shape
        # (see ib_side_channel_trader.py's module docstring).
        #
        # initialize() (where self.ib_aux gets set) only runs once
        # run_bot_in_background's own thread reaches Lumibot's
        # trader.run_all() — fully async relative to this function, so
        # ib_aux doesn't exist on strategy_bot yet at this exact line.
        # Poll briefly rather than reading it immediately (confirmed live
        # 2026-09-03: reading it here unconditionally crashed main() with
        # AttributeError before initialize() had ever run).
        ib_aux_deadline = time.time() + 30
        while not hasattr(strategy_bot, "ib_aux") and time.time() < ib_aux_deadline:
            time.sleep(0.2)
        if getattr(strategy_bot, "ib_aux", None):
            init_extended_hours_trader(strategy_bot.ib_aux)
            init_international_markets_trader(strategy_bot.ib_aux)

        # 2026-09-05: safety-only auto-resume after a watchdog self-heal
        # restart (see scripts/watchdog.sh's SAFETY_RESUME_FLAG_FILE
        # comment for the full "why" — every open position's stop-loss/
        # take-profit/time-limit protection is enforced entirely inside
        # on_trading_iteration, which does nothing while is_trading is
        # False, and a restart never auto-resumed trading before this).
        # Must run AFTER the ib_aux wait above, not before it: initialize()
        # (which sets is_trading=False unconditionally, see its own
        # comment) runs asynchronously in run_bot_in_background's thread —
        # calling start_trading() any earlier than this point would get
        # silently overwritten back to False the moment initialize()
        # actually executes.
        # This marker is written ONLY by watchdog.sh itself, immediately
        # before ITS OWN restart calls — a manual restart (a human running
        # this script directly) never sees it, so manual starts keep
        # requiring an explicit dashboard Start exactly as before.
        # Consumed (deleted) here so it only ever fires once, for the
        # specific restart it was written for.
        safety_resume_flag = os.path.join(os.path.dirname(__file__), ".watchdog_safety_resume")
        if os.path.exists(safety_resume_flag):
            try:
                os.remove(safety_resume_flag)
            except OSError:
                pass
            set_setting("new_entries_paused", 1.0)
            strategy_bot.start_trading()
            logger.warning(
                "Safety-only auto-resume: watchdog restarted this process, so existing "
                "positions' stop-loss/take-profit/time-limit protection resumes immediately. "
                "New entries stay PAUSED until a human explicitly un-pauses them."
            )

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
