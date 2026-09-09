"""Multi-strategy manager for Lumibot trading bot.

Manages:
- Momentum Allocator Strategy
- Scalping Strategy (also scans symbols with a current positive news-sentiment
  spike — see news_sentiment.py — as extra candidates; sentiment is not its
  own independent trading strategy, see 2026-09-01 project memory)
- Breakout Strategy
- Mean Reversion Strategy
- VWAP Strategy
- Gap and Go Strategy
- Reversal Strategy
- Bracket Orders (stop-loss + take-profit)
- Position lifecycle and graceful shutdown
- Signal logging + secondary-indicator confirmation (AI layer, Phase 1)
- Per-strategy enable/disable toggles (dashboard-controlled)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from decimal import Decimal
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor

from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
from lumibot.brokers import InteractiveBrokers
import pandas as pd

import config
import signal_logger
import meta_model
import ib_side_channel_trader
from confirmation import confirm_signal
import asset_class_riders
from regime_indicators import compute_indicators, MIN_BARS_REQUIRED
from regime_matcher import classify_regime, STRATEGY_REGIMES
from news_sentiment import NewsSentimentAnalyzer
from momentum_allocator import MomentumAllocator
from scalping import ScalpingStrategy
from scalping_v2 import ScalpingV2Strategy
from breakout import BreakoutStrategy
from mean_reversion import MeanReversionStrategy
from vwap import VWAPStrategy
from gap_and_go import GapAndGoStrategy
from reversal import ReversalStrategy
from market_profile import MarketProfileStrategy

# 2026-09-04 five-category-pod cutover — see _run_pod_strategies below for
# why only stock_scanner is actually summoned for order placement right
# now (etf_scanner/commodity_scanner/forex_scanner/crypto_scanner exist
# and are imported for their scan/regime logic to be reachable later, but
# their underlying instruments need contract-routing support
# _place_bracket_order doesn't have yet).
import ib_connector
import stock_scanner
import etf_scanner
import commodity_scanner
import forex_scanner
import crypto_scanner
import portfolio_manager
import risk_rules

logger = logging.getLogger(__name__)


class BracketOrder:
    """Represents a bracket order (entry + stop-loss + take-profit)."""

    def __init__(self, symbol: str, entry_price: float, quantity: int,
                 stop_loss_percent: float = None, take_profit_percent: float = None,
                 stop_loss_price: float = None, take_profit_price: float = None,
                 strategy_name: str = None, contract=None, entry_time: datetime = None):
        self.symbol = symbol
        self.entry_price = entry_price
        self.current_price = entry_price
        self.quantity = quantity
        # 2026-09-05 fix: was datetime.now() unconditionally — real wall-
        # clock time, even during a Yahoo backtest whose simulated clock
        # can advance 2 years in ~15 real seconds. That made the
        # time-limit safety valve below structurally unable to ever fire
        # in a backtest (confirmed live: 0/41 trades in a hold-time
        # analysis exited via time_limit, even ones "held" 63 simulated
        # days past a 5-day limit) — every backtest-derived hold time was
        # inflated versus what would actually happen live. Callers now
        # pass self.get_datetime() (the strategy's own clock — real time
        # live/paper, simulated time in a backtest); falls back to
        # datetime.now() only if a caller doesn't pass one.
        self.entry_time = entry_time if entry_time is not None else datetime.now()
        self.strategy_name = strategy_name
        # Real ibapi Contract object — set only for a position this
        # bot's own ib_aux connection owns entry/exit for (international/
        # forex, or a US symbol bought/held outside regular hours).
        # None means "plain US regular-hours position", Lumibot's own
        # broker (get_position/create_order/submit_order) manages it
        # exactly as before this 2026-09-03 consolidation. See
        # ib_side_channel_trader.py's module docstring.
        self.contract = contract
        # Absolute exit-price overrides take precedence — for a strategy
        # whose validated edge is indicator-derived exit levels (e.g.
        # mean_reversion's ATR/SMA20-based exit from experimental_bb_
        # momentum_hedge.py) rather than a fixed percent of entry price.
        if stop_loss_price is not None and take_profit_price is not None:
            self.stop_loss_price = stop_loss_price
            self.take_profit_price = take_profit_price
        else:
            # Per-strategy stop-loss/take-profit if set (signal_logger.
            # get_strategy_risk), otherwise the global default from config.py.
            sl_pct = stop_loss_percent if stop_loss_percent is not None else config.STOP_LOSS_PERCENT
            tp_pct = take_profit_percent if take_profit_percent is not None else config.TAKE_PROFIT_PERCENT
            self.stop_loss_price = entry_price * (1 - sl_pct)
            self.take_profit_price = entry_price * (1 + tp_pct)
        self.entry_order_id = None
        self.stop_loss_order_id = None
        self.take_profit_order_id = None
        self.status = "PENDING"  # PENDING, FILLED, CLOSED

    def __repr__(self):
        return f"BracketOrder({self.symbol} x{self.quantity} @ ${self.entry_price:.2f})"


# Bounded concurrency for _prewarm_prices — enough to meaningfully
# overlap IB's per-symbol lookup latency (each up to max_wait_time,
# see bot_runner.py) without flooding IB Gateway with simultaneous
# snapshot requests (these are one-shot snapshots, not held live
# subscriptions, so this is a courtesy bound, not a hard IB limit).
PREWARM_MAX_WORKERS = 10

# 2026-09-04 five-category-pod cutover (see _run_pod_strategies) — how
# often the pod scan/regime pipeline runs. Each pod's historical-bar scan
# is real sequential-per-symbol-if-not-pooled IB work; ib_aux's own
# international/forex scan (a much bigger universe, ~100-200 symbols per
# market) is throttled to 1800s for the same reason. Pod universes are
# smaller (5-25 symbols each), so twice as often is reasonable, but still
# far slower than the 60s core cycle — running this every cycle would
# reintroduce the exact sequential-IB-call iteration-time blowout
# (63s->120s+) fixed earlier the same day this was built.
POD_SCAN_INTERVAL_SECONDS = 900
POD_SCAN_MAX_WORKERS = 10  # matches PREWARM_MAX_WORKERS — same reasoning

# 2026-09-04 (round 4, general mechanism, not a one-off): the round-robin
# above already guarantees at most one CATEGORY scans per cycle, but a
# single category can itself be heavy — ETF grew to 35 instruments today,
# the largest of the five pods (Stock 25, Commodity 14, Forex 3, Crypto
# 4). A category whose universe exceeds this splits into multiple
# smaller (category, chunk_index) scan units that round-robin
# independently (see _pod_scan_units below) — the SAME due-picking
# machinery, just working over finer-grained units when one category is
# large enough to need it. User's own framing ("if any of the pod is
# getting heavy") asked for this to keep applying automatically to
# whatever turns out heavy later, not just today's known offender.
# Sized off real evidence the same day: Germany's 15-symbol scan alone
# took 1.44s; Australia's 199-symbol scan alone took 14.65s — a 25-item
# cap keeps any single turn's scan cost in the low single digits of
# seconds even before order-placement cost stacks on top.

# 2026-09-04: lowered 25->12 after a real cycle with 25 still stalled
# at 182s (barely over 180s, but over) — halving the per-turn scan
# volume trims that remaining margin without another round of
# guessing. A single turn at 25 already scanned fine in isolation
# (Australia's first 25-symbol chunk was well under 1s); the stall
# came from this cost stacking with the rest of a normal cycle
# (regular 8-strategy scan + pod order-placement + lot-size retries),
# so shrinking the chunk further directly buys back margin there.
POD_MAX_UNIVERSE_PER_TURN = 12

# 2026-09-04 stall fix: bounds the order-PLACEMENT phase (real,
# sequential IB round trips -- quote + currency conversion + submit --
# one per candidate, unlike the scan phase which is already pooled).
# Confirmed live: a single cycle that scanned all five pods at once
# produced 10 real BUY candidates and blew past the 180s watchdog
# threshold trying to place all of them in one iteration. Any extra
# candidates beyond this cap are simply reconsidered on that
# categorys next scan (candidates are recomputed fresh each time,
# never queued), so this only delays them, it never drops one.
#
# Lowered 3->2 same day (round 2): the per-order round trip was
# assumed ~15s (REQUEST_TIMEOUT_SECONDS) when this cap was first set,
# but two REAL orders (SGLN, SGLD -- both immediately PreSubmitted
# then canceled by IB since LSE was closed) each actually took ~32s,
# and the cycle still blew past 180s and got killed a second time.
# The LSE-closed case itself is now skipped before ever attempting an
# order (see the market-open check below), but 2 stays the safer cap
# for whatever's left after that -- the existing 8-strategy scan alone
# has been observed up to ~95s on its own; 95 + pod-scan overhead +
# 2x32s leaves real margin under 180s, 3x32s did not.
POD_MAX_ORDERS_PER_CYCLE = 2

# 2026-09-04: which of the five pods' BUY candidates actually get placed
# as real orders. STOCK routes through _place_bracket_order (unchanged —
# plain US-domestic, the one shape it already handles correctly). ETF/
# COMMODITY/FOREX/CRYPTO route through self.ib_aux._place_entry directly,
# with a real contract built from the pod's own Instrument definition
# (ib_connector.py's Instrument->Contract logic) — this is the SAME
# _place_entry the international/forex regime scanner already uses live,
# routed by instrument shape (contract.secType/currency via _app_for_
# contract), not by current US session time the way _place_bracket_
# order's extended-hours branch is. See _run_pod_strategies for the full
# routing. All five are still gated behind their own per-category
# strategy_toggles entry (pod_stock/pod_etf/pod_commodity/pod_forex/
# pod_crypto), seeded OFF by default — this set controls what CAN place
# real orders once a toggle is turned on, not what's currently enabled.
POD_ORDER_CATEGORIES = frozenset({"STOCK", "ETF", "COMMODITY", "FOREX", "CRYPTO"})

# category -> (scanner module, its *_UNIVERSE list) — every pod uses the
# same scan_instrument(connector, instrument)/scan_universe(connector,
# universe) shape (see each module's own docstring), so this is the only
# per-pod-specific lookup _run_pod_strategies needs.
POD_MODULES = {
    "STOCK": (stock_scanner, stock_scanner.STOCK_UNIVERSE),
    "ETF": (etf_scanner, etf_scanner.ETF_UNIVERSE),
    "COMMODITY": (commodity_scanner, commodity_scanner.COMMODITY_UNIVERSE),
    "FOREX": (forex_scanner, forex_scanner.FOREX_UNIVERSE),
    "CRYPTO": (crypto_scanner, crypto_scanner.CRYPTO_UNIVERSE),
}


class MultiStrategyBot(Strategy):
    """Main strategy class that coordinates multiple trading strategies."""

    def initialize(self):
        """Initialize the multi-strategy bot."""
        # YahooDataBacktesting only has daily bars, so a 60-minute sleeptime
        # during backtests re-runs every strategy ~7x per trading day against
        # the identical daily bar, logging duplicate, non-independent signals.
        # Live/paper trading gets real intraday data, so it checks every 60
        # seconds. IMPORTANT: this must be a string with an explicit unit —
        # Lumibot's own convention interprets a bare int as MINUTES, not
        # seconds (see strategy_executor.py's calculate_strategy_trigger).
        # A bare `60` here silently meant "60 minutes" in live trading
        # (discovered 2026-08-31: iterations appeared to randomly "hang" for
        # up to ~25 minutes at a time; they were actually just waiting out a
        # real ~60-minute cycle, with short gaps being artifacts of restarts
        # forcing an immediate run via Lumibot's own scheduler-setup reset).
        self.sleeptime = "1D" if getattr(self, "is_backtesting", False) else "60S"

        # 2026-09-03 (user call, "only one bot handling everything"): run
        # continuously Monday-Friday instead of Lumibot's default NASDAQ
        # calendar (09:30-16:00 ET only, sleeping the rest of the time —
        # confirmed live via the literal log line "Sleeping until the
        # market opens"). '24/5' is Lumibot's own built-in market string
        # (see Strategy.set_market's docstring) — every strategy here
        # already gates its OWN entries by session/hours internally
        # (_place_bracket_order routes through the IB aux connection's
        # LMT+outsideRth path outside 09:30-16:00 ET; the international/
        # forex scan has its own per-market hours), so this just lets the
        # loop itself run instead of legitimately sleeping through the
        # entire pre-market/post-market window it's now able to trade.
        self.set_market("24/5")

        # Make sure the signal-logging database exists (safe to call repeatedly)
        signal_logger.init_db()

        # Strategy states
        # Backtests have no dashboard to click "Start" on, so auto-enable
        # trading when running as a backtest. Live/paper trading still
        # requires an explicit START from the dashboard.
        self.is_trading = bool(getattr(self, "is_backtesting", False))
        self.shutdown_requested = False

        # Heartbeat: when the trading loop last actually started/finished
        # an iteration. is_trading alone doesn't prove the loop is still
        # ticking — it can stay True while the loop thread is silently
        # hung, with the Flask API thread remaining responsive throughout
        # (observed live on 2026-08-31: status stayed "trading" for 22+
        # minutes with zero iterations actually running). Exposed via
        # /api/status so a stall is visible without grepping logs.
        self.last_iteration_started_at = None
        self.last_iteration_completed_at = None
        self.trading_started_at = None

        # Initialize sub-strategies
        self.momentum_allocator = MomentumAllocator(self)
        self.news_sentiment = NewsSentimentAnalyzer(self)
        self.scalping = ScalpingStrategy(self)
        self.scalping_v2 = ScalpingV2Strategy(self)
        self.breakout = BreakoutStrategy(self)
        self.mean_reversion = MeanReversionStrategy(self)
        self.vwap = VWAPStrategy(self)
        self.gap_and_go = GapAndGoStrategy(self)
        self.reversal = ReversalStrategy(self)
        self.market_profile = MarketProfileStrategy(self)

        # IB-only auxiliary connection (2026-09-03 consolidation) —
        # international markets (LSE/ASX), forex, and US extended-hours
        # order routing, all driven synchronously from this bot's own
        # on_trading_iteration now (see ib_side_channel_trader.py's
        # module docstring for why this used to be a separate background
        # thread with its own schedule, and why that's gone). None for a
        # non-IB broker (Alpaca, backtests) — this whole mechanism is
        # IB-specific by nature (outsideRth orders, non-US contracts).
        self.ib_aux = ib_side_channel_trader.IBAuxTrader(self) if isinstance(self.broker, InteractiveBrokers) else None

        # 2026-09-04 five-category-pod cutover — a SEPARATE IB Gateway
        # connection (own clientId) purely for the five pods' read-only
        # market-data scanning (ib_connector.py's own request-tracking
        # shape, not self.ib_aux's — the two modules are independent
        # implementations of the same reqId-keyed pattern, built
        # separately earlier the same day). Deliberately NOT used for
        # order placement (see POD_ORDER_CATEGORIES / _run_pod_strategies)
        # — using order_executor.py's own direct-placement path there
        # would make this a THIRD, uncoordinated order-placement engine
        # alongside the main Lumibot broker path and self.ib_aux, the
        # exact multi-engine problem retired earlier today. Object created
        # here (cheap, no connection yet); connected lazily inside
        # _run_pod_strategies only if at least one pod toggle is on — no
        # reason to hold open a connection nothing will ever use.
        self.pod_connector = (
            ib_connector.IBConnector(
                config.INTERNATIONAL_MARKETS_IP, int(config.INTERNATIONAL_MARKETS_PORT),
                client_id=6, forex_client_id=7,
            ) if isinstance(self.broker, InteractiveBrokers) else None
        )
        # 2026-09-04 stall fix: was a single shared timestamp gating
        # ALL five pods together — on cold start (bot just restarted)
        # every category is simultaneously "overdue", so the first
        # cycle with N pods enabled scanned+allocated+placed orders for
        # all N at once. Confirmed live 2026-09-04: with all five on,
        # one cycle produced 10 real BUY candidates across categories,
        # took >189s, and watchdog.sh correctly killed it as a genuine
        # stall. Fix: per-category timestamps + a round-robin cursor so
        # at most ONE category's scan (plus a capped number of order
        # placements, see POD_MAX_ORDERS_PER_CYCLE) runs per cycle, even
        # when every category is due at once.
        self._pod_category_last_scan: Dict[str, float] = {}
        self._pod_scan_cursor = 0

        # Track bracket orders
        self.bracket_orders: Dict[str, BracketOrder] = {}
        self.closed_positions: List[Dict] = []
        # Real-fill-price closed-trade ledger context (2026-09-03) — see
        # on_filled_order and _close_position. Every _close_position
        # caller deletes self.bracket_orders[symbol] SYNCHRONOUSLY right
        # after calling it (before the async sell-fill confirmation can
        # possibly arrive), so on_filled_order can't rely on the bracket
        # still being there when the real exit fill lands. _close_position
        # snapshots what it needs here first; on_filled_order pops it
        # once the real exit price is known and writes the completed
        # trade to signal_logger.log_closed_trade.
        self._pending_trade_closes: Dict[str, Dict] = {}
        # Liquidations triggered by _check_news_emergency_stop, in-memory
        # only — reset each time the bot restarts. See that method.
        self.news_emergency_stops: List[Dict] = []

        # Symbol -> capital committed by an order placed earlier in the
        # *current* trading iteration. All 8 strategies run sequentially
        # every iteration and each independently sizes against buying
        # power, which doesn't reflect an order until it fills — without
        # this, several strategies firing in the same cycle would each see
        # the same "full" buying power and could over-commit capital, or
        # two strategies could both buy the same symbol in the same cycle
        # before either order registers as a position. Reset every
        # iteration (see on_trading_iteration); also doubles as the
        # same-cycle "already ordered this symbol" lock.
        self._cycle_reservations: Dict[str, float] = {}

        # (symbol, length, timestep) -> Bars, cleared every iteration (see
        # on_trading_iteration and the get_historical_prices override
        # below). Added 2026-08-31 after widening the symbol universes
        # caused an Alpaca rate-limit hit: the 6 fixed-universe strategies
        # now overlap heavily (AAPL/MSFT/TSLA/NVDA/SPY/QQQ/MU appear in
        # most of their lists), and each was independently re-fetching the
        # same symbol's bars from Alpaca every cycle — this cache means
        # each (symbol, length, timestep) combination is fetched at most
        # once per iteration no matter how many strategies need it.
        self._bars_cache: Dict[tuple, object] = {}

        # Same idea, for get_last_price() — added 2026-09-02. Unlike
        # historical bars this had NO caching at all: every strategy
        # independently calls get_last_price(symbol) for every symbol in
        # its own decisions dict every cycle, with heavy overlap (AAPL,
        # MSFT, TSLA, NVDA, MU, JPM all appear in multiple strategies'
        # universes), plus api.py's dashboard formatting calls it again
        # on every poll. Each uncached call is a real synchronous IB
        # round trip; found live to be a real contributor to iterations
        # occasionally running long enough to starve Flask past the
        # watchdog's reachability window (STALL DETECTED: API
        # unreachable, recurring all day even after the account-summary
        # and watchdog-timeout fixes). A ~60-70s-stale price within one
        # cycle is fine — every consumer already treats it as an
        # approximate reference (order execution is at-market, not
        # limit), and using one consistent price per symbol per cycle is
        # more internally consistent, not less correct, same rationale
        # as the historical-bars cache above.
        self._last_price_cache: Dict[str, float] = {}

        # Per-cycle FX-rate cache for _run_pod_strategies' currency
        # conversion (ib_connector.convert_currency) — same reasoning as
        # _last_price_cache above: N pod candidates needing the same
        # currency pair in one cycle should cost one real quote, not N,
        # but a rate is volatile enough that it shouldn't outlive one
        # cycle either.
        self._fx_rate_cache: dict = {}

        # 2026-09-09: real, persistent record of every time IB reported a
        # buying_power/portfolio_value implausible for this account's
        # real size (config.CAPITAL_SANITY_THRESHOLD_GBP) -- see that
        # constant's own comment and project_100k_tsla_incident_and_
        # hard_ceiling_fix memory for the real incident this exists to
        # catch immediately instead of after the fact. Surfaced via
        # /api/capital-anomalies.
        self.capital_anomalies = []

        # Performance tracking
        self.trade_history = []
        self.strategy_performance = {
            "momentum": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
        }
        self.strategy_performance.update({
            "scalping": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "scalping_v2": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "breakout": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "mean_reversion": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "vwap": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "gap_and_go": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "reversal": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
        })

        # Phase 4: meta-model confidence gate stats (fired vs filtered),
        # in-memory only — reset each time the bot restarts.
        self.confidence_stats = {
            name: {"fired": 0, "filtered": 0} for name in signal_logger.ALL_STRATEGY_NAMES
        }

        # 2026-09-01: per-symbol regime-precedence stats (times a strategy
        # lost a same-cycle, same-symbol contest to a better-regime-matched
        # competitor), in-memory only — reset each time the bot restarts.
        # See _resolve_regime_precedence / regime_matcher.STRATEGY_REGIMES.
        # Replaced an earlier same-day hard per-strategy regime block
        # (blocked a strategy from a symbol outright whenever its own
        # regime didn't match, regardless of whether any other strategy
        # even wanted that symbol) — found too aggressive in a preview run
        # (e.g. reversal blocked to 2/14 of its own universe) and replaced
        # with this softer precedence-only version same day.
        self.regime_precedence_stats = {
            name: {"preempted": 0} for name in signal_logger.ALL_STRATEGY_NAMES
        }

        if self.ib_aux:
            # Connect eagerly here, not lazily on the international/forex
            # cycle's first run inside on_trading_iteration — that cycle
            # only runs while self.is_trading is True, but the Test All
            # Strategies sweep (api.py's _run_test_sweep) deliberately
            # calls _place_bracket_order directly with bypass_pause_gate,
            # independent of is_trading/Start, specifically so it can be
            # used while paused. Found live 2026-09-03: an extended-hours
            # test-sweep entry failed with IB error 504 "Not connected"
            # because ib_aux had never connected — Start had never been
            # clicked, so the lazy connect point was never reached.
            #
            # Placed at the END of initialize(), not right after
            # self.ib_aux is created — reconcile_positions() registers
            # brackets via _register_aux_bracket, which needs
            # self.bracket_orders (and other per-cycle dicts below it)
            # to already exist. Found live 2026-09-03: placing this
            # eagerly right after ib_aux's own construction crashed with
            # "'MultiStrategyBot' object has no attribute
            # 'bracket_orders'" — reconciliation silently failed on
            # every boot, which is why 3 real test-sweep positions
            # (TSLA/AAPL/MU) showed avg_fill_price=0.0 on the dashboard
            # even though their real IB cost basis was intact.
            try:
                self.ib_aux.connect()
                self.ib_aux.reconcile_positions()
            except Exception as e:
                logger.error(f"Error connecting ib_aux at startup: {e}")

        logger.info("MultiStrategyBot initialized")

    def get_historical_prices(self, asset, length, timestep="", *args, **kwargs):
        """Per-iteration cache over Lumibot's own get_historical_prices —
        see _bars_cache above for why. Only caches the plain (asset,
        length, timestep) case with no extra args/kwargs (timeshift,
        quote, exchange, etc.), which is the only way anything in this
        codebase actually calls it; anything fancier bypasses the cache
        and goes straight to the real fetch, so it never returns a wrong
        answer, just an uncached one.
        """
        if args or kwargs:
            return super().get_historical_prices(asset, length, timestep, *args, **kwargs)

        key = (str(asset), length, timestep)
        if key in self._bars_cache:
            return self._bars_cache[key]

        bars = super().get_historical_prices(asset, length, timestep)
        self._bars_cache[key] = bars
        return bars

    def get_last_price(self, asset, *args, **kwargs):
        """Per-iteration cache over Lumibot's own get_last_price — see
        _last_price_cache above for why. Only caches the plain
        get_last_price(symbol) case with no extra args/kwargs (quote,
        exchange), which is the only way anything in this codebase
        actually calls it; anything fancier bypasses the cache and goes
        straight to the real fetch. NOT reset by liquidation/shutdown —
        only on_trading_iteration's own reset (matching _bars_cache)
        controls its lifetime, so a stale-but-recent price is the worst
        case, never a stuck-forever one.
        """
        if args or kwargs:
            return super().get_last_price(asset, *args, **kwargs)

        key = str(asset)
        if key in self._last_price_cache:
            return self._last_price_cache[key]

        price = super().get_last_price(asset)
        self._last_price_cache[key] = price
        return price

    def get_positions(self, include_cash_positions: bool = False, broker_refresh: bool = True,
                       broker_refresh_ttl_seconds: float = 3.0):
        """Defaults broker_refresh_ttl_seconds to 3s — Lumibot's own
        default is 0.0 (always re-hits the broker). Found live
        2026-09-03: a single 120.64s on_trading_iteration included
        THREE separate "The queue was empty or max time reached for
        positions" timeouts (this account's IB positions lookup can
        itself take the full max_wait_time to fail/return) — on_trading_
        iteration's own calls (news emergency stop, bracket-price
        updates) and concurrent dashboard-polling threads (api.py's
        /api/status, /api/positions, hit every ~1-2s per connected
        dashboard tab) were all independently re-fetching with zero
        reuse. Lumibot's own Broker.refresh_positions(ttl_seconds=...)
        (see lumibot/brokers/broker.py) already implements exactly the
        caching this needs — nothing here called it. This just changes
        the default; any caller that explicitly passes its own
        broker_refresh_ttl_seconds (e.g. wanting guaranteed-fresh data
        before a liquidation) still gets exactly what it asks for.
        """
        return super().get_positions(include_cash_positions, broker_refresh, broker_refresh_ttl_seconds)

    def _cycle_symbol_universe(self) -> set:
        """Every symbol this cycle's price lookups will plausibly need:
        each of the 8 strategies' own universe, plus every currently
        open position (bracket_orders). Only used to decide what
        _prewarm_prices should fetch ahead of time — never changes what
        actually gets analyzed or traded.
        """
        symbols = set(self.bracket_orders.keys())
        symbols.update(config.MOMENTUM_UNIVERSE)
        for sub in (self.scalping, self.scalping_v2, self.breakout, self.mean_reversion, self.vwap,
                    self.gap_and_go, self.reversal, self.market_profile):
            symbols.update(getattr(sub, "symbols", None) or [])
        return symbols

    def _prewarm_prices(self, symbols) -> None:
        """Concurrently pre-fetch quotes for `symbols` via self.ib_aux's
        already-connected SideChannelApp, writing straight into
        self._last_price_cache so get_last_price()'s existing per-cycle
        cache override (above) picks them up for free — every strategy's
        own sequential get_last_price(symbol) call this cycle then hits
        the cache instead of making its own blocking IB round trip.

        Built 2026-09-03 after set_market('24/5') made on_trading_
        iteration run continuously (not just 09:30-16:00 ET) and
        iteration times grew from ~0.1s (no-op, pre-Start) to 63s-120s+
        real cycles — traced to 15-20+ SEQUENTIAL per-symbol IB lookups
        each taking up to max_wait_time (see bot_runner.py) before
        falling back to yesterday's close. This overlaps them instead of
        running them one after another.

        Deliberately does NOT call Lumibot's own get_tick()/
        get_last_price() concurrently — read interactive_brokers.py's
        IBWrapper.tickPrice and confirmed it keeps ALL in-flight state
        on single shared instance attributes (self.bid, self.ask,
        self.price, self.tick_asset, self.my_tick_queue — see
        IBClient.get_tick()), not keyed by reqId. Two concurrent calls
        through that path could genuinely cross-contaminate each other's
        results (thread A's ask price silently overwritten by thread B's
        incoming tick before A's tickSnapshotEnd fires) — a real
        correctness risk, not just a performance one, for a live trading
        bot. self.ib_aux's SideChannelApp is safe for this instead: its
        get_req_id() is lock-protected and its _req_results/_req_events
        are plain dicts keyed by a unique reqId per call, so concurrent
        requests never share mutable state — this is the same object
        this codebase already relies on for real order placement
        (verified thread-safe, see ib_side_channel_trader.py).

        IB-only (self.ib_aux is None for Alpaca/backtests, a no-op
        there). A symbol that fails to pre-warm just falls through to
        the normal (slower but correct) Lumibot path when a strategy
        actually asks for it — no regression, only upside.
        """
        if not self.ib_aux or not self.ib_aux.connected:
            return
        to_fetch = [s for s in symbols if s not in self._last_price_cache]
        if not to_fetch:
            return

        def _fetch_one(symbol):
            try:
                # 2026-09-04: a position's own stored contract
                # (BracketOrder.contract, set by _register_aux_bracket for
                # any non-US-stock entry — international equities, ETFs,
                # commodities, forex, crypto) needs a DIFFERENT fetch
                # method than a plain US stock. Found live in two steps:
                # (1) plain_us_contract() for an HK/ASX ticker (e.g.
                # "6862", "1211", "3988", "LOV") is simply the wrong
                # contract, so pre-warm silently failed for every
                # international position, falling through to Lumibot's
                # OWN default-contract path later in the same cycle,
                # which pays for the identical mistake with a blocking
                # ~5s timeout retried twice (06:57:37-06:58:12 "Unable to
                # get data" errors, ~10-15s per symbol). (2) fixing the
                # contract wasn't enough — self.ib_aux._get_live_quote()
                # (reqMktData tick snapshot) genuinely returns (None,
                # None) for these specific SEHK/ASX symbols even with the
                # correct contract (confirmed via had_contract=True,
                # bid=None, ask=None diagnostic logging), while
                # reconcile_positions() has always priced these same
                # symbols successfully via _fetch_bars() (reqHistoricalData
                # — a different, more reliable data channel for
                # delayed/thin-subscription symbols than a live tick
                # snapshot). So: a symbol with a stored international
                # contract is priced via _fetch_bars' latest close,
                # exactly like reconciliation does; a plain US-universe
                # symbol keeps using the live quote as before (proven
                # reliable there for days).
                existing = self.bracket_orders.get(symbol)
                if existing and existing.contract:
                    contract = existing.contract
                    # 2026-09-04: _fetch_bars hardcodes secType="STK" —
                    # correct for a stock/ETF/commodity contract (the
                    # case this branch was originally written for), but
                    # silently wrong for FOREX (CASH) or CRYPTO, which
                    # this branch now also reaches since it's keyed on
                    # "has a stored contract" not "is a stock." Found
                    # live: a real EURUSD forex position opened via the
                    # pod pipeline started failing this same fetch every
                    # cycle, falling through to cache-miss -> Lumibot's
                    # own broken default get_last_price("EURUSD") path
                    # (~10s of "Unable to get data for EURUSD" retries
                    # per cycle) — the identical class of bug the STK
                    # fix above this comment was written for, just for a
                    # contract type it never accounted for. Forex/crypto
                    # use a live quote instead (the already-proven path
                    # real forex/crypto entries and check_exits' own
                    # _quote_forex both use), not a historical-bars fetch.
                    if contract.secType in ("CASH", "CRYPTO"):
                        bid, ask = self.ib_aux._get_live_quote(contract)
                        price = (bid + ask) / 2 if bid and ask else (bid or ask)
                        return (symbol, float(price)) if price else (symbol, None)
                    df = self.ib_aux._fetch_bars(
                        symbol, contract.primaryExchange, contract.currency, duration="2 D"
                    )
                    if df is not None and len(df) > 0:
                        return symbol, float(df.iloc[-1]["close"])
                    return symbol, None

                bid, ask = self.ib_aux._get_live_quote(ib_side_channel_trader.plain_us_contract(symbol))
                price = bid or ask
                if price:
                    return symbol, float(price)
            except Exception as e:
                logger.debug(f"Price pre-warm failed for {symbol}: {e}")
            return symbol, None

        with ThreadPoolExecutor(max_workers=PREWARM_MAX_WORKERS) as pool:
            for symbol, price in pool.map(_fetch_one, to_fetch):
                if price is not None:
                    self._last_price_cache[symbol] = price

    def _register_aux_bracket(self, symbol: str, contract, quantity, entry_price: float,
                               stop_price: float, target_price: float, strategy_name: str):
        """Called BY self.ib_aux (reconciliation, international/forex
        entries, US extended-hours entries) to record a real fill into
        the ONE position book (self.bracket_orders) — see BracketOrder's
        `contract` field and ib_side_channel_trader.py's module
        docstring. Kept here (not built inside ib_side_channel_trader.py)
        so BracketOrder construction stays in one place; avoids a
        circular import between the two modules too."""
        self.bracket_orders[symbol] = BracketOrder(
            symbol, entry_price, quantity,
            stop_loss_price=stop_price, take_profit_price=target_price,
            strategy_name=strategy_name, contract=contract,
            entry_time=self.get_datetime(),
        )

    def _check_confidence(self, strategy_name: str, features: Dict) -> bool:
        """Meta-model confidence gate (Phase 4). Applied only to BUY signals
        that already passed secondary-indicator confirmation. Strategies
        without a trained model (see meta_model.py / train_meta_model.py)
        pass through ungated — there's no confidence score to gate on yet.
        """
        min_confidence = signal_logger.get_setting("min_signal_confidence", 0.6)
        confidence = meta_model.get_confidence(strategy_name, features, confirmed=True)
        stats = self.confidence_stats.setdefault(strategy_name, {"fired": 0, "filtered": 0})

        if confidence is None or confidence >= min_confidence:
            stats["fired"] += 1
            return True

        stats["filtered"] += 1
        logger.info(
            f"{strategy_name} BUY filtered by meta-model confidence "
            f"({confidence:.2f} < {min_confidence:.2f})"
        )
        return False

    def get_confidence_stats(self) -> Dict:
        return self.confidence_stats

    def _resolve_regime_precedence(self, all_decisions: Dict[str, Dict[str, str]]):
        """Per-symbol regime precedence (2026-09-01). Runs once per cycle,
        BEFORE any order is placed, over every strategy's raw BUY decisions
        for this cycle. When 2+ strategies want to BUY the SAME symbol in
        the SAME cycle, the one whose regime_matcher.STRATEGY_REGIMES entry
        matches that symbol's CURRENT regime (ADX-based, computed per
        symbol — an individual symbol routinely trends against the broad
        market, so this is never a single shared "market status") wins;
        the others are preempted for that symbol THIS CYCLE ONLY — they're
        free to trade it again next cycle if no better-matched competitor
        shows up then. No contention, or an ambiguous match (zero or 2+ of
        the contending strategies match the regime), changes nothing —
        falls back to the pre-existing fixed-order same-cycle lock
        (_cycle_reservations) exactly as it worked before this feature.

        Returns (preempted, contested, resolved):
          - preempted: (strategy_name, symbol) pairs to skip when placing
            orders (the signal is still logged as normal either way).
          - contested: symbols where 2+ strategies wanted to BUY this cycle,
            regardless of whether precedence could actually resolve a
            winner.
          - resolved: the subset of `contested` where exactly one contender
            matched the regime (a real winner was decided) — as opposed to
            an AMBIGUOUS contested symbol (0 or 2+ matches, e.g. any of the
            6 strategies sharing the "bullish" assignment overlapping each
            other), where nobody is preempted and nothing was really
            decided. Distinguishing these matters for signals.db tagging:
            with 6/8 strategies sharing one regime, most real contention IS
            ambiguous, so "not preempted" alone would call every contender
            in an ambiguous cycle a "winner" even though precedence never
            actually resolved anything there.
        """
        buyers_by_symbol: Dict[str, list] = {}
        for strategy_name, decisions in all_decisions.items():
            for symbol, action in (decisions or {}).items():
                if action == "BUY":
                    buyers_by_symbol.setdefault(symbol, []).append(strategy_name)

        contested = {symbol for symbol, buyers in buyers_by_symbol.items() if len(buyers) >= 2}
        resolved = set()

        preempted = set()
        for symbol, buyers in buyers_by_symbol.items():
            if len(buyers) < 2:
                continue  # no contention, nothing to resolve

            bars = self.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
            if not bars or len(bars.df) < MIN_BARS_REQUIRED:
                continue  # can't classify regime — leave contention to the existing lock

            indicators = compute_indicators(bars.df)
            regime, adx = classify_regime(indicators)
            if regime is None:
                continue

            matched = [s for s in buyers if STRATEGY_REGIMES.get(s) == regime]
            if len(matched) != 1:
                continue  # ambiguous (0 or 2+ matches) — no clear president

            resolved.add(symbol)
            president = matched[0]
            for strategy_name in buyers:
                if strategy_name == president:
                    continue
                preempted.add((strategy_name, symbol))
                stats = self.regime_precedence_stats.setdefault(strategy_name, {"preempted": 0})
                stats["preempted"] += 1
                logger.info(
                    f"{strategy_name} BUY for {symbol} preempted by {president} this cycle "
                    f"(current regime={regime}/adx={adx:.1f} matches {president}, not {strategy_name})"
                )

        return preempted, contested, resolved

    def get_regime_precedence_stats(self) -> Dict:
        return self.regime_precedence_stats

    @staticmethod
    def _regime_precedence_state(name: str, symbol: str, contested: set, resolved: set, preempted: set) -> str:
        """Categorical signals.db tag for this cycle's precedence outcome —
        see _resolve_regime_precedence's docstring for why "contested but
        ambiguous" must stay distinct from "contested, resolved, and won"
        rather than collapsing both into a single not-preempted boolean."""
        if symbol not in contested:
            return "uncontested"
        if symbol not in resolved:
            return "ambiguous"
        return "resolved_loser" if (name, symbol) in preempted else "resolved_winner"

    def preview_all_decisions(self) -> Dict:
        """Read-only diagnostic: re-run every sub-strategy's .analyze() right
        now and return its raw decisions, without logging, confirming,
        gating, or placing any order. Used to reconcile against signals.db —
        if this says a strategy currently wants to BUY a symbol but no
        matching row was logged in signals.db recently, that points at a
        bug in the live iteration path (a swallowed exception, a stale
        universe, etc.) rather than a legitimate no-signal condition, since
        every real BUY decision _run_strategy/_run_momentum_strategy
        produces gets logged unconditionally before confirmation or the
        confidence gate are ever applied. "sentiment" isn't its own
        strategy (2026-09-01) — its raw analyze() output is shown here
        anyway since it's still a useful read of what the sentiment scan
        currently sees, even though it no longer trades independently.
        """
        sub_strategies = {
            "momentum": self.momentum_allocator,
            "sentiment": self.news_sentiment,
            "scalping": self.scalping,
            "scalping_v2": self.scalping_v2,
            "breakout": self.breakout,
            "mean_reversion": self.mean_reversion,
            "vwap": self.vwap,
            "gap_and_go": self.gap_and_go,
            "reversal": self.reversal,
            "market_profile": self.market_profile,
        }
        result = {}
        for name, sub in sub_strategies.items():
            try:
                decisions = sub.analyze() or {}
                result[name] = {"decisions": decisions}
            except Exception as e:
                result[name] = {"error": str(e)}
        return result

    def on_trading_iteration(self):
        """Main trading loop executed every sleeptime interval."""
        try:
            # Check for graceful shutdown
            if self.shutdown_requested:
                self._liquidate_all_positions()
                self.is_trading = False
                logger.info("Bot shutdown complete")
                return

            if not self.is_trading:
                return

            self.last_iteration_started_at = datetime.now()

            # Fresh cycle: clear the per-iteration historical-bars cache
            # (see get_historical_prices override below).
            self._bars_cache = {}
            self._last_price_cache = {}
            self._fx_rate_cache = {}

            # Fresh cycle: no capital committed and no symbols locked yet.
            self._cycle_reservations = {}

            # Concurrently pre-fetch prices for every symbol this cycle
            # will plausibly need — see _prewarm_prices' own docstring
            # for why this exists and why it's safe. Runs BEFORE any of
            # the price lookups below so they all benefit.
            self._prewarm_prices(self._cycle_symbol_universe())

            # Update position prices
            self._update_bracket_order_prices()

            # Check if any bracket orders hit stop-loss or take-profit
            self._check_bracket_orders()

            # Liquidate ANY open position (any strategy) on severe news —
            # see _check_news_emergency_stop's own docstring for why this
            # is separate from and complementary to the price-based checks
            # above.
            self._check_news_emergency_stop()

            # Collect every enabled strategy's raw decisions up front, before
            # any order is placed, so same-cycle regime-precedence conflicts
            # between strategies can be resolved first (see
            # _resolve_regime_precedence). A disabled strategy's analyze()
            # is skipped entirely, same as before this feature existed —
            # it never contributes to precedence contention either.
            # "sentiment" isn't in this dict — it's not an independent
            # trading strategy (2026-09-01): scalping.py calls into
            # self.news_sentiment itself to pull in symbols with a current
            # positive sentiment spike as extra scan candidates, so
            # sentiment's contribution already shows up inside
            # all_decisions["scalping"] rather than as its own entry.
            sub_strategies = {
                "momentum": self.momentum_allocator,
                "scalping": self.scalping,
                "scalping_v2": self.scalping_v2,
                "breakout": self.breakout,
                "mean_reversion": self.mean_reversion,
                "vwap": self.vwap,
                "gap_and_go": self.gap_and_go,
                "reversal": self.reversal,
                "market_profile": self.market_profile,
            }
            all_decisions = {
                name: (sub.analyze() if signal_logger.is_strategy_enabled(name) else {})
                for name, sub in sub_strategies.items()
            }
            preempted, contested, resolved = self._resolve_regime_precedence(all_decisions)

            # Run momentum strategy
            self._run_momentum_strategy(all_decisions["momentum"], preempted, contested, resolved)

            # Run additional strategies (logging + confirmation + toggle
            # checks are all applied uniformly inside _run_strategy)
            self._run_strategy("scalping", all_decisions["scalping"], preempted, contested, resolved)
            self._run_strategy("scalping_v2", all_decisions["scalping_v2"], preempted, contested, resolved)
            self._run_strategy("breakout", all_decisions["breakout"], preempted, contested, resolved)
            self._run_strategy("mean_reversion", all_decisions["mean_reversion"], preempted, contested, resolved)
            self._run_strategy("vwap", all_decisions["vwap"], preempted, contested, resolved)
            self._run_strategy("gap_and_go", all_decisions["gap_and_go"], preempted, contested, resolved)
            self._run_strategy("reversal", all_decisions["reversal"], preempted, contested, resolved)
            self._run_strategy("market_profile", all_decisions["market_profile"], preempted, contested, resolved)

            # Rebalance portfolio
            self._rebalance_portfolio()

            # International markets + forex — a distinct regime-based
            # decision algorithm (see ib_side_channel_trader.py's
            # _evaluate_entry), not the 8 named strategies above, so it's
            # not part of all_decisions. Self-throttled internally to
            # INTERNATIONAL_LOOP_INTERVAL_SECONDS and gated on its own
            # dashboard toggle — safe/cheap to call every iteration.
            # Extended-hours US entries need no separate call here: they
            # already went through _run_momentum_strategy/_run_strategy
            # above like any other cycle, and _place_bracket_order
            # itself detects non-regular-hours + IB and routes through
            # self.ib_aux automatically (see that method).
            if self.ib_aux:
                try:
                    if not self.ib_aux.connected:
                        self.ib_aux.connect()
                        self.ib_aux.reconcile_positions()
                    self.ib_aux.run_international_and_forex_cycle()
                except Exception as e:
                    logger.error(f"Error in IB aux cycle: {e}", exc_info=True)

            # Five-category pods (2026-09-04 cutover) — a NEW candidate-
            # generation source alongside the 8 named strategies above,
            # off by default per pod, self-throttled internally. See
            # _run_pod_strategies' own docstring for the full design.
            self._run_pod_strategies()

            self.last_iteration_completed_at = datetime.now()
            logger.debug("Trading iteration complete")

        except Exception as e:
            self.last_iteration_completed_at = datetime.now()
            logger.error(f"Error in trading iteration: {e}", exc_info=True)

    def _run_momentum_strategy(self, decisions: Dict[str, str], preempted: set, contested: set, resolved: set):
        """Execute momentum allocator strategy, with signal logging,
        secondary-indicator confirmation, and regime-precedence gating BUY
        entries.

        Args:
            decisions: this cycle's already-computed momentum_allocator
                .analyze() result (see on_trading_iteration — computed
                once up front so _resolve_regime_precedence can see every
                strategy's decisions before any order is placed).
            preempted: (strategy_name, symbol) pairs this cycle's
                _resolve_regime_precedence decided lose to a better-
                regime-matched competitor wanting the same symbol.
        """
        try:
            if not decisions:
                return

            available_capital = self._get_available_capital(
                config.MOMENTUM_ALLOCATION_PERCENT
            )
            scores = self.momentum_allocator.get_scores()

            for symbol, action in decisions.items():
                current_price = self.get_last_price(symbol)
                features = {"momentum_score": scores.get(symbol)}

                if action == "BUY":
                    confirmed, confirm_features = confirm_signal(
                        self, symbol, action, strategy_name="momentum"
                    )
                    features.update(confirm_features)
                    features["regime_precedence_state"] = self._regime_precedence_state(
                        "momentum", symbol, contested, resolved, preempted
                    )
                    score = features.get("momentum_score")
                    features["reason"] = (
                        f"Momentum score {score:.2f}" if score is not None
                        else "Momentum allocator signal"
                    )
                    signal_logger.log_signal(
                        "momentum", symbol, action, features,
                        entry_price=current_price, confirmed=confirmed,
                        timestamp=self.get_datetime(),
                    )
                    if confirmed and self._check_confidence("momentum", features) and ("momentum", symbol) not in preempted:
                        self._place_bracket_order(symbol, "buy", available_capital, strategy_name="momentum")
                        logger.info(f"momentum strategy triggered BUY for {symbol} (confirmed)")
                    elif confirmed and ("momentum", symbol) in preempted:
                        logger.info(f"momentum BUY for {symbol} preempted by a better-regime-matched strategy this cycle, skipping")
                    elif confirmed:
                        logger.info(f"momentum BUY for {symbol} filtered by meta-model confidence, skipping")
                    else:
                        logger.info(
                            f"momentum BUY for {symbol} not confirmed by secondary indicator, skipping"
                        )
                elif action == "SELL":
                    # Exits are never blocked by confirmation — but an
                    # exit signal only means something if there's an
                    # actual position to close. Logging it unconditionally
                    # (previously hardcoded confirmed=True) flooded Recent
                    # Activity with "confirmed" SELLs for symbols never
                    # held — found live 2026-09-02 when the user noticed
                    # dozens of confirmed sells with no real trade ever
                    # executed. Every strategy evaluates its exit
                    # condition against its whole universe every cycle
                    # regardless of holdings, so this was pure noise, not
                    # a bug in the exit logic itself.
                    if self.get_position(symbol):
                        features["reason"] = "Momentum allocator exit signal"
                        signal_logger.log_signal(
                            "momentum", symbol, action, features,
                            entry_price=current_price, confirmed=True,
                            timestamp=self.get_datetime(),
                        )
                        self._close_position(symbol, strategy_name="momentum")

        except Exception as e:
            logger.error(f"Error in momentum strategy: {e}", exc_info=True)

    def _run_strategy(self, name, decisions, preempted: set, contested: set, resolved: set):
        """Execute a generic strategy given its buy/sell decisions.

        Applies, uniformly for all six simple strategies (scalping,
        breakout, mean_reversion, vwap, gap_and_go, reversal):
          - signal logging (every decision, confirmed or not)
          - secondary-indicator confirmation gating BUY entries only
          - regime-precedence gating BUY entries only

        Args:
            name: Strategy name (used for logging, toggles, confirmation
                  type lookup, and performance tracking)
            decisions: this cycle's already-computed decisions (see
                on_trading_iteration — the enable/disable toggle is applied
                there, before analyze() is even called, same as before this
                cycle-wide precomputation existed; an empty dict here means
                either no signal or the strategy is disabled)
            preempted: (strategy_name, symbol) pairs this cycle's
                _resolve_regime_precedence decided lose to a better-
                regime-matched competitor wanting the same symbol
        """
        try:
            if not decisions:
                return

            available_capital = self._get_available_capital(config.MAX_POSITION_SIZE)

            for symbol, action in decisions.items():
                current_price = self.get_last_price(symbol)
                features = {}

                if action == "BUY":
                    confirmed, confirm_features = confirm_signal(
                        self, symbol, action, strategy_name=name
                    )
                    features.update(confirm_features)
                    features["regime_precedence_state"] = self._regime_precedence_state(
                        name, symbol, contested, resolved, preempted
                    )
                    # Optional per-strategy reason hook (currently only
                    # scalping.get_signal_reason, for news-sentiment-rider-
                    # sourced entries) — falls back to a generic description
                    # so every logged signal has a human-readable "reason"
                    # for the dashboard's Recent Activity feed.
                    strategy_obj_for_reason = getattr(self, name, None)
                    get_reason = getattr(strategy_obj_for_reason, "get_signal_reason", None)
                    reason = get_reason(symbol) if get_reason else None
                    features["reason"] = reason or f"{name.replace('_', ' ').title()} entry signal"
                    signal_logger.log_signal(
                        name, symbol, action, features,
                        entry_price=current_price, confirmed=confirmed,
                        timestamp=self.get_datetime(),
                    )
                    if confirmed and self._check_confidence(name, features) and (name, symbol) not in preempted:
                        # A strategy whose validated edge is an indicator-
                        # derived exit (ATR/SMA20/etc.) rather than a fixed
                        # percent of entry price exposes get_exit_levels() —
                        # mean_reversion/vwap/reversal always do; scalping
                        # only does for THIS symbol if it was a sentiment-
                        # rider-sourced BUY this cycle (its base-universe
                        # BUYs return None, falling back to the uniform
                        # per-strategy risk% below, same as before). See
                        # BracketOrder's absolute-price override.
                        sl_price = tp_price = None
                        strategy_obj = getattr(self, name, None)
                        get_levels = getattr(strategy_obj, "get_exit_levels", None)
                        if get_levels:
                            levels = get_levels(symbol)
                            if levels:
                                sl_price, tp_price = levels
                        # Optional per-strategy position-sizing hook
                        # (currently only market_profile.get_position_
                        # notional) — validated 2026-09-02 in
                        # experimental_market_profile_filters.py: sizing
                        # so every trade risks roughly the same dollar
                        # amount (rather than a flat percent-of-portfolio
                        # notional regardless of stop distance) dropped
                        # profit concentration in the top 10 of 475 trades
                        # from 92.0% to 47.4% and improved total P&L ~14%
                        # over 90 days of real data. Never returns MORE
                        # than the normal allocation, only less — a
                        # conservative live adaptation of the sandbox's
                        # own (uncapped-upward) validation.
                        order_capital = available_capital
                        get_notional = getattr(strategy_obj, "get_position_notional", None)
                        if get_notional and sl_price is not None:
                            order_capital = get_notional(symbol, available_capital, current_price, sl_price)
                        self._place_bracket_order(
                            symbol, "buy", order_capital, strategy_name=name,
                            stop_loss_price=sl_price, take_profit_price=tp_price,
                        )
                        logger.info(f"{name} strategy triggered BUY for {symbol} (confirmed)")
                    elif confirmed and (name, symbol) in preempted:
                        logger.info(f"{name} BUY for {symbol} preempted by a better-regime-matched strategy this cycle, skipping")
                    elif confirmed:
                        logger.info(f"{name} BUY for {symbol} filtered by meta-model confidence, skipping")
                    else:
                        logger.info(
                            f"{name} BUY for {symbol} not confirmed by secondary indicator, skipping"
                        )
                elif action == "SELL":
                    # Exit signal only means something if there's an
                    # actual position to close — see the matching
                    # comment in the momentum SELL branch above for why
                    # this check was added (was flooding Recent Activity
                    # with "confirmed" sells for symbols never held).
                    if self.get_position(symbol):
                        features["reason"] = f"{name.replace('_', ' ').title()} exit signal"
                        signal_logger.log_signal(
                            name, symbol, action, features,
                            entry_price=current_price, confirmed=True,
                            timestamp=self.get_datetime(),
                        )
                        self._close_position(symbol, reason=name, strategy_name=name)
                        logger.info(f"{name} strategy triggered SELL for {symbol}")

        except Exception as e:
            logger.error(f"Error in {name} strategy: {e}", exc_info=True)

    def _place_bracket_order(self, symbol: str, side: str, available_capital: float,
                              strategy_name: str = None,
                              stop_loss_price: float = None, take_profit_price: float = None,
                              bypass_pause_gate: bool = False):
        """Place a bracket order (entry + stop-loss + take-profit).

        Args:
            symbol: Stock symbol
            side: "buy" or "sell"
            available_capital: Available capital for this trade
            strategy_name: which strategy triggered this order — looks up
                that strategy's own stop-loss/take-profit (falls back to
                the global default if it has no override; see
                signal_logger.get_strategy_risk).
            stop_loss_price/take_profit_price: absolute price overrides for
                strategies with indicator-derived exit levels instead of a
                fixed percent (e.g. mean_reversion). Both must be given
                together, or neither.
            bypass_pause_gate: ONLY for api.py's Test All Strategies sweep
                (2026-09-02) — that feature requires new entries to
                already be paused before it can even start (the dashboard
                button is disabled otherwise), so without this it could
                never place a single order, defeating its own purpose.
                Every real strategy call site leaves this False and stays
                fully gated.
        """
        try:
            # New-entries pause (2026-09-02, user-requested transition
            # control) — every real call site here is an entry (confirmed:
            # only ever called with side="buy"), so gating here covers
            # both the momentum and generic-strategy paths in one place.
            # Exits (bracket stop/target, strategy SELL signals, etc.)
            # never go through this method, so they're unaffected.
            if not bypass_pause_gate and signal_logger.get_setting("new_entries_paused", 0.0) >= 0.5:
                logger.info(f"New entries paused, skipping {symbol} ({strategy_name})")
                return

            # Outside regular US hours (IB only) — Lumibot's own broker
            # never sets the outsideRth order flag (confirmed by reading
            # lumibot/brokers/interactive_brokers.py's IBClient.
            # create_order directly), so a plain order placed here would
            # just sit unfilled until the next regular session. Route
            # through self.ib_aux's LMT+outsideRth path instead — a
            # completely different, smaller-notional, thinner-liquidity-
            # aware order shape, so this is its own branch rather than a
            # tweak to the regular-hours path below. 2026-09-03
            # consolidation: this used to be a separate background scan
            # cycle (ib_side_channel_trader.py's old _run_us_extended_
            # hours_cycle) calling the SAME 8 strategies' analyze() a
            # second time; now this is the ONLY entry path, reached
            # naturally since set_market('24/5') keeps on_trading_
            # iteration running through pre/post-market too.
            is_ib = isinstance(self.broker, InteractiveBrokers)
            session = ib_side_channel_trader.us_session() if is_ib else "regular"
            if is_ib and session != "regular" and side == "buy":
                # No separate enable/disable toggle any more (2026-09-03,
                # user call) — extended-hours trading is always on
                # whenever the bot itself is trading, same as regular
                # hours.
                if not self.ib_aux:
                    return
                if self.get_position(symbol) or symbol in self.bracket_orders:
                    logger.info(f"Already have position in {symbol}, skipping")
                    return
                if symbol in self._cycle_reservations:
                    logger.info(f"{symbol} already ordered by another strategy this cycle, skipping")
                    return
                self.ib_aux.place_extended_hours_entry(
                    symbol, strategy_name or "unknown",
                    stop_price=stop_loss_price, target_price=take_profit_price,
                )
                return

            # Get current price
            current_price = self.get_last_price(symbol)
            if not current_price:
                logger.warning(f"Could not get price for {symbol}")
                return

            # Degenerate setup guard, matching experimental_bb_momentum_
            # hedge.py's sandbox logic: skip if the indicator-derived exit
            # levels don't actually bracket the current price (can happen
            # when price has moved since the strategy computed them).
            if stop_loss_price is not None and take_profit_price is not None:
                if take_profit_price <= current_price or stop_loss_price >= current_price:
                    logger.info(
                        f"Degenerate dynamic exit levels for {symbol} "
                        f"(stop={stop_loss_price:.2f}, target={take_profit_price:.2f}, "
                        f"price={current_price:.2f}), skipping"
                    )
                    return

            # Fractional position sizing — a truncated int() size silently
            # places zero shares (and thus no trade at all) whenever
            # available_capital is less than one share's price, which is
            # exactly the failure mode a small account hits on an
            # expensive stock. Alpaca supports fractional share quantities.
            #
            # Interactive Brokers does NOT (confirmed live 2026-09-02 via
            # IB error 10243, "Fractional-sized order cannot be placed via
            # API. Please use desktop version to place this order.") —
            # this was silently rejecting EVERY buy order, from every
            # strategy, all day since the IB migration (available_capital
            # / current_price is fractional for virtually any real trade).
            # Lumibot logs the rejection but never surfaces it as an
            # actionable order failure, so it just looked like an
            # untraceable "missing order" with no fill and no error.
            # Round down to a whole share on IB; keep true fractional
            # sizing on Alpaca, which supports it natively.
            position_size = available_capital / current_price
            if isinstance(self.broker, InteractiveBrokers):
                position_size = float(int(position_size))
            if position_size <= 0:
                logger.warning(
                    f"Position size for {symbol} is 0 (capital=${available_capital:.2f}, "
                    f"price=${current_price:.2f}), skipping order"
                )
                return

            # Check if we already have a position
            existing_position = self.get_position(symbol)
            if existing_position and side == "buy":
                logger.info(f"Already have position in {symbol}, skipping")
                return

            # Another strategy already ordered this symbol earlier in this
            # same iteration — its order likely hasn't filled yet, so the
            # position check above wouldn't have caught it.
            if side == "buy" and symbol in self._cycle_reservations:
                logger.info(
                    f"{symbol} already ordered by another strategy this cycle, skipping"
                )
                return

            # Place entry order. time_in_force must be "day" — Alpaca
            # rejects fractional-quantity orders ("fractional orders must
            # be DAY orders") under the default "gtc", and position_size
            # here is always fractional (see the sizing comment above).
            # Found live 2026-08-31: every BUY that passed confirmation +
            # confidence had been silently failing at the broker for this
            # reason — _check_confidence's "fired" counter increments
            # before this call, so it was never actually proof an order
            # went through.
            entry_order = self.create_order(symbol, position_size, side, time_in_force="day")
            self.submit_order(entry_order)

            # Alpaca can reject an order synchronously (e.g. invalid
            # symbol, insufficient buying power) — don't reserve capital or
            # lock the symbol for an order that never actually went live.
            # A rejection reported asynchronously later is handled in
            # on_canceled_order below.
            if entry_order.is_canceled():
                logger.warning(
                    f"{side} order for {symbol} was rejected/canceled immediately, not reserving capital"
                )
                return

            logger.info(f"Placed {side} order for {position_size:.4f}x {symbol} @ ${current_price:.2f}")

            if side == "buy":
                self._cycle_reservations[symbol] = position_size * current_price

            # Create bracket order tracking
            risk = signal_logger.get_strategy_risk(strategy_name) if strategy_name else {}
            bracket = BracketOrder(
                symbol, current_price, position_size,
                stop_loss_percent=risk.get("stop_loss_percent"),
                take_profit_percent=risk.get("take_profit_percent"),
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                strategy_name=strategy_name,
                entry_time=self.get_datetime(),
            )
            # This Lumibot version's Order object exposes `.identifier`,
            # not `.id` — using getattr with a fallback so this degrades
            # gracefully instead of crashing if the attribute name changes
            # again in a future version.
            bracket.entry_order_id = getattr(entry_order, "identifier", None)
            self.bracket_orders[symbol] = bracket

        except Exception as e:
            logger.error(f"Error placing bracket order for {symbol}: {e}")

    def on_canceled_order(self, order):
        """Lumibot lifecycle hook — fires when a submitted order is
        canceled or rejected by the broker. Alpaca doesn't always reject
        synchronously (the immediate check in _place_bracket_order only
        catches instant rejections), so this releases a same-cycle capital
        reservation/symbol lock for a rejection that arrives after the
        fact — otherwise that capital and symbol would stay blocked from
        other strategies for the rest of the cycle even though the order
        never actually went through.
        """
        try:
            symbol = getattr(getattr(order, "asset", None), "symbol", None)
            order_id = getattr(order, "identifier", None)
            if not symbol or symbol not in self._cycle_reservations:
                return

            bracket = self.bracket_orders.get(symbol)
            if bracket and bracket.entry_order_id == order_id:
                logger.warning(
                    f"Entry order for {symbol} was canceled/rejected by the broker, "
                    f"releasing capital reservation and symbol lock"
                )
                del self._cycle_reservations[symbol]
                del self.bracket_orders[symbol]

        except Exception as e:
            logger.error(f"Error handling canceled order: {e}")

    def on_filled_order(self, position, order, price, quantity, multiplier):
        """Lumibot lifecycle hook — fires with the REAL broker fill price
        (2026-09-03), unlike the signal-time reference price everything
        else here is computed from. Two jobs:
        1. Entry (buy) fill: correct this symbol's BracketOrder.entry_
           price from its approximate reference price to the real fill,
           so the eventual closed-trade P&L is accurate.
        2. Exit (sell) fill: complete the closed_trades record started
           by _close_position's _pending_trade_closes snapshot (see that
           method) and write it via signal_logger.log_closed_trade — the
           single source of truth behind Performance Overview / Strategy
           Performance / Recent Closed Orders.
        """
        try:
            symbol = getattr(getattr(order, "asset", None), "symbol", None)
            if not symbol:
                return
            side = str(getattr(order, "side", "")).lower()

            if side == "buy":
                bracket = self.bracket_orders.get(symbol)
                if bracket and bracket.entry_order_id == getattr(order, "identifier", None):
                    bracket.entry_price = price
                return

            if side == "sell":
                ctx = self._pending_trade_closes.pop(symbol, None)
                if not ctx or ctx["entry_price"] is None:
                    # No captured context (e.g. a position that was
                    # already open before this process started, closed
                    # with no bracket AND no broker avg_fill_price
                    # available either) — nothing accurate to record,
                    # skip rather than log a trade with a fabricated
                    # entry price.
                    return
                # 2026-09-09 fix: was datetime.now() -- real wall-clock
                # time, not the strategy's own clock. Harmless live (the
                # two are nearly identical in real-time trading), but
                # during a BACKTEST this recorded a 2024 opened_at next
                # to a 2026 (today's real date) closed_at -- every
                # backtest trade's "hold time" computed from these two
                # fields was a meaningless real-world-elapsed number, not
                # the actual simulated hold duration. get_datetime() is
                # this project's own established pattern for exactly this
                # (see reference_lumibot_api_quirks memory) -- correct in
                # both live and backtest, returns the strategy's own
                # current clock either way.
                signal_logger.log_closed_trade(
                    strategy=ctx["strategy"],
                    symbol=symbol,
                    entry_price=ctx["entry_price"],
                    exit_price=price,
                    quantity=quantity,
                    close_reason=ctx["reason"],
                    opened_at=ctx["opened_at"],
                    closed_at=self.get_datetime(),
                    side_channel=False,
                )

        except Exception as e:
            logger.error(f"Error in on_filled_order: {e}")

    def _check_bracket_orders(self):
        """Check if any bracket orders have hit stop-loss/take-profit, or
        outlived the time-limit safety valve."""
        # Aux-owned brackets (bracket.contract is not None — international/
        # forex, or a US symbol currently outside regular hours) get their
        # stop/target/weekend-buffer checked here instead, via IBAuxTrader.
        # check_exits — Lumibot's own get_last_price/get_position below
        # can't correctly resolve a non-US contract at all, and a plain US
        # symbol outside regular hours needs LMT+outsideRth to actually
        # close (see ib_side_channel_trader.py's module docstring).
        if self.ib_aux:
            try:
                self.ib_aux.check_exits()
            except Exception as e:
                logger.error(f"Error checking IB aux exits: {e}", exc_info=True)
        try:
            default_time_limit_days = signal_logger.get_setting("time_limit_days", 5.0)
            for symbol, bracket in list(self.bracket_orders.items()):
                if bracket.contract is not None:
                    continue  # handled by self.ib_aux.check_exits() above
                current_price = self.get_last_price(symbol)
                if not current_price:
                    continue

                # Per-strategy override (e.g. mean_reversion's sandbox-
                # validated 10-day max hold, vs. the 5-day global default),
                # stored as a plain "time_limit_days__{strategy}" key in
                # the same signal_settings table get_setting/set_setting
                # already use — no schema change needed.
                time_limit_days = (
                    signal_logger.get_setting(
                        f"time_limit_days__{bracket.strategy_name}", default_time_limit_days
                    )
                    if bracket.strategy_name else default_time_limit_days
                )

                # Check take-profit. Uses .pop(symbol, None) rather than
                # del — 2026-09-03: _close_position can now route this
                # symbol through self.ib_aux (a plain US bracket held
                # past regular hours) and pop bracket_orders[symbol]
                # itself on a real fill, which would make a bare `del`
                # here raise KeyError.
                if current_price >= bracket.take_profit_price:
                    logger.info(
                        f"Take-profit hit for {symbol} @ ${current_price:.2f}"
                    )
                    self._close_position(symbol, "take_profit")
                    self.bracket_orders.pop(symbol, None)

                # Check stop-loss
                elif current_price <= bracket.stop_loss_price:
                    logger.info(
                        f"Stop-loss hit for {symbol} @ ${current_price:.2f}"
                    )
                    self._close_position(symbol, "stop_loss")
                    self.bracket_orders.pop(symbol, None)

                # Time-limit safety valve for the exclusive ownership lock
                # in _close_position — a symbol shouldn't stay locked to
                # one strategy forever just because neither its own SELL
                # signal nor price target ever fires again. Falls back to
                # the same global time_limit_days label_signals.py already
                # uses for retrospective labeling if no per-strategy
                # override is set (see time_limit_days__{strategy} above).
                elif self.get_datetime() - bracket.entry_time > timedelta(days=time_limit_days):
                    logger.info(
                        f"Time limit ({time_limit_days}d) reached for {symbol}, closing"
                    )
                    self._close_position(symbol, "time_limit")
                    self.bracket_orders.pop(symbol, None)

        except Exception as e:
            logger.error(f"Error checking bracket orders: {e}")

    def _check_news_emergency_stop(self):
        """Portfolio-wide safety net (2026-09-01, user call): liquidate ANY
        open position — regardless of which strategy opened it — the moment
        genuinely severe negative news shows up for that symbol. Checked
        every cycle for every real open position (not just ones this bot
        is tracking in self.bracket_orders, so nothing engaged is missed).

        This is deliberately NOT the same as a per-strategy entry filter —
        that was tested for mean_reversion specifically (block its own new
        entries on bad news) and found redundant with its 5-day-momentum
        filter (see mean_reversion.py's module docstring): sharp price
        drops and bad news are largely the same events at ENTRY time, so a
        pre-entry news filter adds no independent value there. But for a
        position that's already open, price hasn't necessarily moved yet
        when the news lands — momentum can't help there, only a reactive
        check like this one can. Uses NewsSentimentAnalyzer's own short-
        window, short-cache emergency lookup (see its
        _get_emergency_sentiment_score) — deliberately fresher than the 24h
        window the scalping rider's relevance check uses.

        No strategy_name is passed to _close_position — same pattern
        _check_bracket_orders' stop-loss/take-profit checks use, so this
        always bypasses the exclusive-ownership lock: a portfolio-wide risk
        event isn't a competing strategy's opinion, it must always be able
        to act regardless of who opened the position.
        """
        try:
            positions = self.get_positions()
            if not positions:
                return

            for position in positions:
                symbol = position.symbol
                score = self.news_sentiment._get_emergency_sentiment_score(symbol)
                if score > config.NEWS_EMERGENCY_STOP_THRESHOLD:
                    continue

                logger.warning(
                    f"NEWS EMERGENCY STOP: {symbol} sentiment={score:.2f} "
                    f"(<= {config.NEWS_EMERGENCY_STOP_THRESHOLD}) — liquidating immediately"
                )
                self.news_emergency_stops.append({
                    "symbol": symbol,
                    "sentiment_score": score,
                    "timestamp": datetime.now().isoformat(),
                })
                self._close_position(symbol, reason="news_emergency_stop")
                if symbol in self.bracket_orders:
                    del self.bracket_orders[symbol]

        except Exception as e:
            logger.error(f"Error checking news emergency stop: {e}")

    def get_news_emergency_stops(self) -> List[Dict]:
        return list(self.news_emergency_stops)

    def _close_position(self, symbol: str, reason: str = "manual", strategy_name: str = None) -> bool:
        """Close a position and record trade.

        Args:
            symbol: Stock symbol
            reason: Reason for closing ("take_profit", "stop_loss", "manual", etc)
            strategy_name: which strategy is asking for this close, if any.
                Only strategy-driven SELL signals pass this — bracket-order
                stop-loss/take-profit checks (_check_bracket_orders) don't,
                since those are the position's own risk management, not a
                competing strategy's opinion, and must always be allowed
                through regardless of the ownership lock below.

        Returns:
            True only if a real sell order was actually submitted this
            call, False for every no-op path (no position, ownership-
            locked, same-cycle-deferred, or an error). Added 2026-09-02
            after api.py's Test All Strategies sweep blindly resubmitted
            a close every 3s without checking whether a PRIOR submission
            was still settling — IB can take longer than 3s to reflect a
            fill in get_position(), so the sweep kept re-selling the same
            already-shrinking position and drove NVDA to a -6785-share
            short before it was caught and flattened. Callers that retry
            on a still-open position (like that sweep) MUST stop
            resubmitting once this returns True and switch to polling
            get_position() only — see api.py's _run_test_sweep.
        """
        try:
            position = self.get_position(symbol)
            if not position:
                logger.warning(f"No position to close for {symbol}")
                return False

            # Exclusive ownership: whichever strategy opened a position on
            # a symbol keeps it until it closes naturally (its own SELL
            # signal, or the bracket order's stop-loss/take-profit) — no
            # other strategy may touch it in the meantime. First strategy
            # to signal on a symbol wins; added 2026-08-31 after observing
            # a real whipsaw where breakout kept buying TSLA and reversal
            # kept selling it right back, every single cycle, burning
            # transaction costs with no economic benefit (user-requested
            # design; a smarter "which strategy has better odds on this
            # symbol" priority is a natural future upgrade once enough
            # real trade data exists, but there isn't enough yet).
            bracket = self.bracket_orders.get(symbol)
            if strategy_name and bracket and bracket.strategy_name and bracket.strategy_name != strategy_name:
                logger.info(
                    f"{symbol} is locked to {bracket.strategy_name} (opened it) — "
                    f"{strategy_name}'s SELL signal is blocked until it exits naturally"
                )
                return False

            # A symbol bought earlier THIS SAME cycle (see
            # _cycle_reservations) may not have settled with the broker
            # yet even though get_position() already shows it — Alpaca
            # rejects an immediate opposing order as a "potential wash
            # trade" in that case (confirmed live 2026-08-31: a fresh
            # breakout BUY on TSLA, then reversal's standing SELL signal
            # for TSLA tried to close it 2 seconds later in the same
            # cycle and got rejected with error code 40310000). Defer the
            # close to the next cycle instead — the position isn't going
            # anywhere, and bracket-order stop-loss/take-profit checks
            # (which only ever act on positions from prior cycles) aren't
            # affected by this.
            if symbol in self._cycle_reservations:
                logger.info(
                    f"{symbol} was bought earlier this same cycle, deferring close "
                    f"({reason}) to next cycle to avoid a wash-trade rejection"
                )
                return False

            # Route through self.ib_aux instead of Lumibot's own plain
            # sell whenever Lumibot's order path can't correctly handle
            # this position: a real international/forex contract
            # (bracket.contract is set), or a plain US symbol currently
            # outside regular hours (Lumibot never sets outsideRth — a
            # plain sell placed outside 09:30-16:00 ET would just sit
            # unfilled). ib_aux._close_position logs the real closed-
            # trade record itself (these orders never go through
            # Lumibot's on_filled_order lifecycle) and pops
            # self.bracket_orders[symbol] on a real fill — every caller
            # here that also does bracket_orders.pop/del afterward is
            # safe either way (see _check_bracket_orders' .pop(symbol,
            # None) calls).
            is_ib = isinstance(self.broker, InteractiveBrokers)
            session = ib_side_channel_trader.us_session() if is_ib else "regular"
            needs_aux = (bracket is not None and bracket.contract is not None) or (is_ib and session != "regular")
            if needs_aux and self.ib_aux:
                self.ib_aux._close_position(symbol, reason)
                return symbol not in self.bracket_orders

            # Snapshot what on_filled_order will need to write a real
            # closed_trades record, BEFORE submitting the sell order —
            # every caller of this method deletes self.bracket_orders
            # right after it returns, so that context won't exist by the
            # time the async exit fill confirmation actually arrives.
            # Falls back to the position's own avg_fill_price if there's
            # no bracket at all (e.g. a position still open from before
            # this process last restarted).
            fallback_entry = float(position.avg_fill_price) if getattr(position, "avg_fill_price", None) else None
            self._pending_trade_closes[symbol] = {
                "strategy": (bracket.strategy_name if bracket else strategy_name) or "unknown",
                "entry_price": (bracket.entry_price if bracket else None) or fallback_entry,
                "opened_at": bracket.entry_time if bracket else None,
                "reason": reason,
            }

            # Place the closing order. time_in_force="day" — same
            # fractional-quantity requirement as the entry order in
            # _place_bracket_order (positions here are routinely
            # fractional shares). Side/size derived from the real
            # position's sign (2026-09-04) — was hardcoded "sell" with
            # position.quantity passed as-is, which only ever worked
            # because this bot never intentionally shorts; a position
            # that ended up short by accident (see ib_side_channel_
            # trader.py's matching fix, same root cause: a duplicate
            # close firing twice) would have had this SELL more instead
            # of covering it.
            close_side = "buy" if position.quantity < 0 else "sell"
            sell_order = self.create_order(
                symbol, abs(position.quantity), close_side, time_in_force="day"
            )
            self.submit_order(sell_order)
            logger.info(f"Closed position in {symbol} ({reason})")

            # Record closed position
            self.closed_positions.append({
                "symbol": symbol,
                "quantity": position.quantity,
                "close_reason": reason,
                "close_time": datetime.now(),
            })
            return True

        except Exception as e:
            logger.error(f"Error closing position for {symbol}: {e}")
            return False

    def _update_bracket_order_prices(self):
        """Update current prices for tracked bracket orders."""
        try:
            symbols = list(self.bracket_orders.keys())
            if not symbols:
                return

            for symbol in symbols:
                bracket = self.bracket_orders[symbol]
                current_price = self.get_last_price(symbol)
                if current_price:
                    bracket.current_price = current_price

        except Exception as e:
            logger.error(f"Error updating bracket prices: {e}")

    def _rebalance_portfolio(self):
        """Rebalance portfolio according to strategy allocations."""
        try:
            portfolio_value = self.get_portfolio_value()
            cash = self.get_cash()
            positions = self.get_positions()

            if not positions:
                return

            # Close positions if portfolio is too leveraged
            if cash < config.MIN_CASH_BUFFER * portfolio_value:
                logger.warning("Cash buffer below minimum, considering liquidation")

        except Exception as e:
            logger.error(f"Error rebalancing portfolio: {e}")

    def _run_pod_strategies(self):
        """Five-category pod pipeline (2026-09-04 cutover, extended same
        day to route all five categories, not just STOCK — see below) —
        stock_scanner.py/etf_scanner.py/commodity_scanner.py/forex_
        scanner.py/crypto_scanner.py (scan -> noise filter -> regime ->
        bias) -> portfolio_manager.py (cross-category capital allocation)
        -> risk_rules.py (per-category stop/target) -> order placement,
        so every existing safety mechanism (new-entries pause, already-
        have-position/same-cycle-reservation checks, whole-share
        rounding, P&L logging) applies to a pod-sourced entry identically
        to a strategy-sourced one.

        Deliberately NOT order_executor.py's own execute()/place_order()
        path — that talks to IB directly through ib_connector.py,
        completely bypassing everything above. Using it here would stand
        up a THIRD, uncoordinated order-placement engine alongside the
        main Lumibot broker path and self.ib_aux — exactly the multi-
        engine problem retired earlier the same day this was built, when
        ib_side_channel_trader.py's own independent background thread was
        folded into this one loop ("only one bot handling everything,"
        explicit user direction).

        Two routing paths, split by category, not by session time:
        - STOCK: stock_scanner.py's universe is plain US-domestic (USD,
          SMART-routed, no primaryExchange) — the one shape
          _place_bracket_order already routes correctly (its regular-
          hours path via Lumibot's own order builder, or its extended-
          hours path via self.ib_aux, gated on CURRENT US SESSION TIME —
          correct for this category specifically, since a plain US
          symbol genuinely IS session-gated).
        - ETF/COMMODITY/FOREX/CRYPTO: their instruments are NOT session-
          gated by US market hours at all (LSE has its own hours, forex
          trades near-continuously, crypto is 24/7) — routing them
          through _place_bracket_order's session-time check would
          misbuild the contract regardless of what time it is (confirmed
          by reading its extended-hours branch: it always calls self.
          ib_aux.place_extended_hours_entry, which hardcodes
          plain_us_contract(symbol) internally, wrong for these).
          Instead, route by INSTRUMENT SHAPE directly: build the real
          contract from the pod's own Instrument definition (reusing
          self.pod_connector._build_contract — the exact same Instrument-
          >Contract logic ib_connector.py already uses for scanning, not
          a second, divergent implementation) and call self.ib_aux.
          _place_entry with it — the SAME method the international/forex
          regime scanner already uses live, which already routes purely
          off contract.secType/currency via _app_for_contract (a fix
          this same cutover added: _app_for_contract was missing a
          CRYPTO->forex_app branch entirely, silently misrouting crypto
          orders to the wrong connection). _place_entry's fixed
          NOTIONAL_PER_TRADE_EQUITY/FOREX_UNITS_PER_TRADE sizing is
          overridden with the real portfolio_manager.py-computed
          quantity via new quantity/reservation_notional parameters
          (both None for every OTHER existing caller, so their behavior
          is completely unchanged).

        Currency conversion (2026-09-04, fixed same day this gap was
        flagged): alloc.dollar_amount is a USD figure (get_portfolio_
        value/get_buying_power are both USD). For a non-USD instrument
        (GBP-denominated LSE ETF/commodity), converted to native
        currency via a REAL live forex quote (self.pod_connector.
        convert_currency, config.FOREX_PAIRS' GBP/USD pair) before
        sizing — was previously sized as if dollar_amount were already
        native-currency notional (a $75k allocation treated as £75k).
        order_executor.py's own module docstring still documents the
        same gap in ITS path; not fixed there, only here.

        POD_ORDER_CATEGORIES now includes all five; each is still gated
        independently behind its own strategy_toggles entry (pod_stock/
        pod_etf/pod_commodity/pod_forex/pod_crypto — all seeded OFF).
        Genuinely new, never-live-tested code (the ETF/Commodity/Forex/
        Crypto routing path especially — pod_stock alone was live-
        verified earlier the same day) — the user enables each
        deliberately once they've watched it work.

        Throttled to POD_SCAN_INTERVAL_SECONDS per category, AND
        round-robins across whichever categories are currently due
        rather than running all of them in the same cycle (2026-09-04
        stall fix — see _pod_category_last_scan's own comment in
        __init__: a shared single timestamp meant every enabled
        category was simultaneously due on cold start, and a live
        cycle with all five pods on took >189s and got correctly
        killed by watchdog.sh as a genuine stall). At most one
        category is scanned per cycle now, and POD_MAX_ORDERS_PER_CYCLE
        bounds the order-placement phase too.

        UNVERIFIED end-to-end: this was built and tested with the bot
        process stopped, per standing safety practice (no live order was
        ever placed while writing this) — real wall-clock scan time and
        real order placement have not been observed live for ANY of the
        five categories yet, pod_stock included. Watch the first cycle
        each enabled pod actually scans on closely once Start is clicked.
        """
        try:
            enabled_categories = [
                c for c in POD_ORDER_CATEGORIES
                if signal_logger.is_strategy_enabled(f"pod_{c.lower()}")
            ]
            if not enabled_categories:
                return

            # Expand each enabled category into one or more (category,
            # chunk_index, instruments) scan units — a category over
            # POD_MAX_UNIVERSE_PER_TURN splits into several smaller
            # units (see that constant's own comment). Each unit gets
            # its own key in _pod_category_last_scan/is independently
            # "due", so a large category's chunks rotate through the
            # SAME round-robin cursor as every other unit, not a
            # separate mechanism.
            scan_units = []
            for c in enabled_categories:
                _, universe = POD_MODULES[c]
                if len(universe) <= POD_MAX_UNIVERSE_PER_TURN:
                    scan_units.append((c, 0, universe))
                else:
                    for i in range(0, len(universe), POD_MAX_UNIVERSE_PER_TURN):
                        chunk_idx = i // POD_MAX_UNIVERSE_PER_TURN
                        scan_units.append((c, chunk_idx, universe[i:i + POD_MAX_UNIVERSE_PER_TURN]))

            now = time.time()
            due = [
                u for u in scan_units
                if now - self._pod_category_last_scan.get(f"{u[0]}:{u[1]}", 0.0) >= POD_SCAN_INTERVAL_SECONDS
            ]
            if not due:
                return
            # Only ONE due unit runs this cycle, even when several (or,
            # on cold start, all) are simultaneously overdue -- the
            # actual fix, not just a cap layered on top of the old
            # all-at-once behavior. Cursor rotates fairly among
            # whichever units are due on a given cycle.
            category, chunk_idx, chunk_instruments = due[self._pod_scan_cursor % len(due)]
            self._pod_scan_cursor += 1
            self._pod_category_last_scan[f"{category}:{chunk_idx}"] = now
            enabled_categories = [category]

            if self.pod_connector is None:
                return
            if not self.pod_connector.connected:
                self.pod_connector.connect(connect_forex=True)
            if not self.pod_connector.connected:
                logger.warning("[pods] IB connector not connected, skipping pod scan this cycle")
                return

            # Concurrent per-symbol scan across every enabled pod's
            # universe at once — mirrors _prewarm_prices' exact proven
            # pattern (ib_connector.py's request tracking is the same
            # unique-reqId-per-call shape verified concurrency-safe for
            # that method, built and tested the same way earlier the same
            # day), not a naive sequential scan_universe() call per pod,
            # which would serialize everything and risk the exact
            # iteration-time blowout POD_SCAN_INTERVAL_SECONDS' throttle
            # is trying to keep rare rather than eliminating the cost
            # outright.
            # chunk_instruments, not the category's full universe --
            # this turn only processes its own slice (see the scan-unit
            # expansion above), the rest arrive on later turns.
            module, _ = POD_MODULES[category]
            work = [(category, module, instrument) for instrument in chunk_instruments]

            # 2026-09-05: STOCK/ETF scan_instrument now requires available_
            # capital (a real, live-priced affordability filter — see
            # stock_scanner.py's MIN_SHARES_AFFORDABLE comment for why:
            # IB has no fractional shares, so a symbol priced beyond what
            # current buying power can cover in 3+ whole shares isn't
            # genuinely tradeable, no matter how good its regime/bias
            # looks). COMMODITY/FOREX/CRYPTO scan_instrument signatures are
            # unchanged — forex/futures-style affordability is a currency-
            # conversion and margin-rules problem this cutover doesn't
            # attempt to solve, so those three stay on their original call
            # shape rather than being forced through a filter that
            # wouldn't even be correct for them.
            available_capital = self.get_buying_power() if category in ("STOCK", "ETF") else None

            def _scan_one(item):
                category, module, instrument = item
                try:
                    if category == "STOCK":
                        return category, module.scan_instrument(self.pod_connector, instrument, available_capital)
                    if category == "ETF":
                        # Shares this cycle's own currency-conversion cache
                        # (self._fx_rate_cache, already reused elsewhere
                        # this same iteration) rather than fetching a
                        # fresh USD->native quote per instrument.
                        return category, module.scan_instrument(
                            self.pod_connector, instrument, available_capital,
                            rate_cache=self._fx_rate_cache,
                        )
                    return category, module.scan_instrument(self.pod_connector, instrument)
                except Exception as e:
                    logger.warning(f"[pods] {category} scan failed for {instrument.symbol}: {e}")
                    return category, None

            results_by_category: Dict[str, list] = {}
            with ThreadPoolExecutor(max_workers=POD_SCAN_MAX_WORKERS) as pool:
                for category, result in pool.map(_scan_one, work):
                    if result is not None:
                        results_by_category.setdefault(category, []).append(result)

            candidates = []
            for category, scan_results in results_by_category.items():
                for r in scan_results:
                    if r.status == "ok" and r.bias == "BUY":
                        candidates.append(portfolio_manager.CandidateSignal(
                            symbol=r.symbol, category=category, regime=r.regime,
                        ))
            if not candidates:
                return

            # Same capital figures (get_portfolio_value/get_buying_power/
            # config.MIN_CASH_BUFFER) and the SAME live self._cycle_
            # reservations dict the 8 named strategies' own
            # _get_available_capital already uses — pods and strategies
            # share one pool, not two that could each think they have the
            # full amount. portfolio_manager.allocate() applies its own
            # per-category/per-candidate split ON TOP of this pooled
            # total; it is not a second, independent capital source.
            portfolio_value = self.get_portfolio_value()
            buying_power = self.get_buying_power()
            # 2026-09-04: a transient IB broker-balance sync failure
            # (confirmed live: Lumibot's own _get_balances_at_broker hit
            # a real "list index out of range" — the account summary
            # response was momentarily missing an expected 'BASE'-
            # currency row) leaves get_portfolio_value()/get_buying_
            # power() returning None rather than raising — Lumibot
            # itself just logs it and leaves the cached value alone
            # UNLESS this is the very first successful sync, in which
            # case there's no cached value to fall back to yet. Either
            # way, multiplying None crashed the entire pod cycle outright
            # (caught by the outer try/except, but every candidate this
            # cycle was silently dropped, not just deferred). Skip this
            # cycle cleanly instead — same graceful-degradation pattern
            # _get_available_capital already uses for the 8 named
            # strategies.
            if portfolio_value is None or buying_power is None:
                logger.warning("[pods] Portfolio value/buying power unavailable this cycle (broker sync issue), skipping")
                return
            min_cash = portfolio_value * config.MIN_CASH_BUFFER
            reserved = sum(self._cycle_reservations.values())
            investable_capital = max(0.0, buying_power - min_cash - reserved)
            if investable_capital <= 0:
                logger.info("[pods] No investable capital left this cycle, skipping")
                return

            allocations = portfolio_manager.allocate(candidates, investable_capital, portfolio_value)

            orders_placed_this_cycle = 0
            for alloc in allocations:
                if alloc.category not in POD_ORDER_CATEGORIES or alloc.dollar_amount <= 0:
                    continue
                # 2026-09-04 stall fix: caps the sequential, real-IB-
                # round-trip order-placement phase per cycle (see
                # POD_MAX_ORDERS_PER_CYCLE's own comment) -- the
                # round-robin above already limits a cycle to one
                # category, but that category alone can still produce
                # more candidates than are safe to place sequentially
                # in one iteration. Anything past the cap is simply
                # picked up again next time this category is scanned.
                if orders_placed_this_cycle >= POD_MAX_ORDERS_PER_CYCLE:
                    logger.info(
                        f"[pods] Reached POD_MAX_ORDERS_PER_CYCLE "
                        f"({POD_MAX_ORDERS_PER_CYCLE}), deferring remaining "
                        f"{alloc.category} candidates to next scan"
                    )
                    break
                strategy_name = f"pod_{alloc.category.lower()}"

                if alloc.category == "STOCK":
                    # Plain US-domestic, USD, SMART-routed — the one
                    # shape _place_bracket_order already routes correctly
                    # (regular-hours via Lumibot's own order builder,
                    # extended-hours via self.ib_aux's session-time-gated
                    # path). Unchanged from before this method existed.
                    current_price = self.get_last_price(alloc.symbol)
                    if not current_price:
                        logger.warning(f"[pods] Could not get price for {alloc.symbol}, skipping")
                        continue
                    stop_loss_price, take_profit_price = risk_rules.compute_exit_prices(
                        current_price, "BUY", alloc.category,
                    )
                    self._place_bracket_order(
                        alloc.symbol, "buy", alloc.dollar_amount,
                        strategy_name=strategy_name,
                        stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
                    )
                    orders_placed_this_cycle += 1
                    continue

                # ETF/COMMODITY/FOREX/CRYPTO (2026-09-04): route by
                # INSTRUMENT SHAPE, not session time — self.ib_aux's
                # place_extended_hours_entry (what _place_bracket_order
                # would otherwise call outside regular US hours) only
                # builds plain_us_contract(symbol), wrong for a GBP-
                # denominated LSE ETF or a CASH/CRYPTO contract regardless
                # of what time it is. self.ib_aux._place_entry (the SAME
                # method the international/forex regime scanner already
                # uses live) takes a real, pre-built contract instead and
                # routes purely off that contract's own secType/currency
                # (_app_for_contract) — exactly what these four categories
                # need. Contract built via self.pod_connector's own
                # Instrument->Contract logic (ib_connector.py's
                # _build_contract), reused rather than re-implemented — a
                # Contract/ForexContract object isn't tied to a specific
                # connection, just to matching the right underlying wire
                # library, which _build_contract/_app_for_contract already
                # agree on independently (confirmed by reading both).
                if self.ib_aux is None or self.pod_connector is None:
                    continue
                module, universe = POD_MODULES[alloc.category]
                # Match on .key, not .symbol -- forex_scanner.py's
                # ForexScanResult.symbol is Instrument.key (e.g.
                # "EURUSD", symbol+currency, since USD/JPY and
                # GBP/USD must never collide on plain "USD"/"GBP"),
                # while the other three pods use plain .symbol (e.g.
                # "BTC"). .key falls back to .symbol for every non-
                # forex Instrument, so this one check is correct for
                # all four categories, not a forex-only special case.
                # 2026-09-04 bug, confirmed live: the old .symbol-only
                # match silently skipped every FOREX candidate
                # ("No Instrument definition for EURUSD in FOREX").
                instrument = next(
                    (i for i in universe if i.symbol == alloc.symbol or i.key == alloc.symbol),
                    None,
                )
                if instrument is None:
                    logger.warning(f"[pods] No Instrument definition for {alloc.symbol} in {alloc.category}, skipping")
                    continue

                # Optional asset-class "second opinion" rider (2026-09-05,
                # see asset_class_riders.py) — non-compulsory by design,
                # fails open on any data problem. COMMODITY gets the gold-
                # macro (DXY) check, CRYPTO gets the ETH/BTC rotation check;
                # ETF/FOREX have no rider yet (ETF is equity-space like the
                # 8 named strategies but doesn't share their confirm_signal
                # call path; FOREX's real carry rider is a stub, see
                # check_forex_carry_rider) so both pass through unchanged.
                if alloc.category == "COMMODITY":
                    rider_confirmed, rider_features = asset_class_riders.check_gold_macro_rider()
                elif alloc.category == "CRYPTO":
                    rider_confirmed, rider_features = asset_class_riders.check_crypto_rotation_rider(alloc.symbol)
                else:
                    rider_confirmed, rider_features = True, {}
                if not rider_confirmed:
                    logger.info(f"[pods] {alloc.symbol} ({alloc.category}) BUY skipped by asset-class rider: {rider_features}")
                    continue

                # 2026-09-04 stall fix (round 2, generalized further):
                # a real live cycle placed SGLN then SGLD, both LSE-
                # listed, at ~2am London time — LSE was closed, so IB
                # PreSubmitted then immediately canceled each one (~32s
                # round trip per attempt, not the ~15s REQUEST_TIMEOUT_
                # SECONDS assumed when POD_MAX_ORDERS_PER_CYCLE was
                # sized), and the cycle still blew past 180s and got
                # correctly killed by watchdog.sh a second time. An
                # hours-only check (is_market_open) catches that case,
                # but not a halted security, a symbol IB has no
                # security definition for (seen live all day for
                # several universe symbols), or a market that's open by
                # the clock with no real data flowing — the actual
                # question is "is this genuinely tradeable right now",
                # not just "is the clock right" (2026-09-04, explicit
                # user direction). ib_connector.is_tradeable answers
                # that directly: market-hours check + a real live quote
                # in one call, replacing what used to be two separate
                # steps here (a hours check, then a quote fetch the
                # caller had to remember to also check for None).
                quote = ib_connector.is_tradeable(self.pod_connector, instrument)
                if quote is None:
                    logger.info(f"[pods] {alloc.symbol} not tradeable right now (market closed or no live data), deferring to next scan")
                    continue
                entry_price = quote.ask or quote.last or quote.mid
                if not entry_price or entry_price <= 0:
                    logger.warning(f"[pods] No usable price for {alloc.symbol}, skipping")
                    continue

                # dollar_amount is USD (get_portfolio_value/get_buying_
                # power are both USD figures) — for a non-USD instrument
                # (GBP-denominated LSE ETF/commodity) convert to native
                # currency via a REAL live forex quote before sizing
                # (2026-09-04 fix — this used to size the raw USD figure
                # as if it were already native-currency notional, e.g.
                # treating a $75k allocation as if it were £75k;
                # order_executor.py's own module docstring flagged the
                # same gap, still open there, fixed here).
                notional = instrument.currency
                if notional != "USD":
                    converted = self.pod_connector.convert_currency(
                        alloc.dollar_amount, "USD", notional, rate_cache=self._fx_rate_cache,
                    )
                    if converted is None:
                        logger.warning(
                            f"[pods] No live USD->{notional} rate available, skipping {alloc.symbol}"
                        )
                        continue
                    notional_amount = converted
                else:
                    notional_amount = alloc.dollar_amount
                raw_quantity = notional_amount / entry_price
                quantity = ib_connector._round_quantity(instrument, raw_quantity)
                if quantity <= 0:
                    logger.warning(
                        f"[pods] {alloc.symbol} allocation (${alloc.dollar_amount:.2f} USD "
                        f"= {notional_amount:.2f} {notional} @ {entry_price}) rounds to 0, skipping"
                    )
                    continue

                stop_loss_price, take_profit_price = risk_rules.compute_exit_prices(
                    entry_price, "BUY", alloc.category,
                )
                contract = self.pod_connector._build_contract(instrument)
                self.ib_aux._place_entry(
                    alloc.symbol, contract, entry_price, stop_loss_price, take_profit_price,
                    matched_strategy=strategy_name,
                    quantity=quantity, reservation_notional=alloc.dollar_amount,
                )
                orders_placed_this_cycle += 1
        except Exception as e:
            logger.error(f"Error in pod strategies cycle: {e}", exc_info=True)

    def _liquidate_all_positions(self, exclude_symbols=None):
        """Gracefully liquidate all positions — regular-hours, extended-
        hours, international, and forex alike (2026-09-03 consolidation;
        previously extended-hours/international/forex positions were
        NOT covered by this generic path at all — see git history —
        each aux-owned position below is now routed through self.ib_aux
        instead of a plain Lumibot sell that was never built for it).

        exclude_symbols: still accepted for API-compatibility with
        callers that pre-date the consolidation; not needed for aux-
        owned symbols any more (this method now detects and routes them
        itself), but a caller-specified exclusion is still honored for
        any symbol it genuinely doesn't want touched here.
        """
        logger.info("Starting graceful liquidation of all positions...")
        exclude_symbols = exclude_symbols or set()
        try:
            positions = [p for p in self.get_positions() if p.symbol not in exclude_symbols]
            start_time = time.time()

            # Initialize here so the later check is always valid, even if
            # there were no positions to begin with (previously this could
            # raise UnboundLocalError when `positions` was empty, since the
            # while loop below would never execute).
            remaining_positions = positions

            for position in positions:
                try:
                    bracket = self.bracket_orders.get(position.symbol)
                    is_ib = isinstance(self.broker, InteractiveBrokers)
                    session = ib_side_channel_trader.us_session() if is_ib else "regular"
                    needs_aux = (bracket is not None and bracket.contract is not None) or (is_ib and session != "regular")

                    if needs_aux and self.ib_aux:
                        # Deliberately bypasses _close_position's same-
                        # cycle wash-trade defer — Stop Trading means
                        # "close everything right now", and this only
                        # runs once right before is_trading is set False
                        # (see on_trading_iteration), so there's no next
                        # cycle to retry a deferred close on.
                        self.ib_aux._close_position(position.symbol, "stop_trading")
                        logger.info(f"Liquidated (aux) {position.quantity}x {position.symbol}")
                        continue

                    # Same closed_trades snapshot _close_position takes —
                    # this path bypasses _close_position entirely (submits
                    # the sell order directly), so without this, every
                    # Stop Trading liquidation would be invisible to
                    # Recent Closed Orders/Performance Overview/Strategy
                    # Performance.
                    fallback_entry = float(position.avg_fill_price) if getattr(position, "avg_fill_price", None) else None
                    self._pending_trade_closes[position.symbol] = {
                        "strategy": (bracket.strategy_name if bracket else None) or "unknown",
                        "entry_price": (bracket.entry_price if bracket else None) or fallback_entry,
                        "opened_at": bracket.entry_time if bracket else None,
                        "reason": "stop_trading",
                    }

                    close_side = "buy" if position.quantity < 0 else "sell"
                    sell_order = self.create_order(
                        position.symbol, abs(position.quantity), close_side, time_in_force="day"
                    )
                    self.submit_order(sell_order)
                    logger.info(f"Liquidated {position.quantity}x {position.symbol}")
                except Exception as e:
                    logger.error(f"Error liquidating {position.symbol}: {e}")

            # Wait for orders to fill (with timeout). Re-apply the same
            # exclusion — otherwise this would wait the full timeout for
            # excluded (e.g. international) symbols that were never asked
            # to close here, and log a false "still open" warning about
            # them below.
            while time.time() - start_time < config.GRACEFUL_SHUTDOWN_TIMEOUT:
                remaining_positions = [p for p in self.get_positions() if p.symbol not in exclude_symbols]
                if not remaining_positions:
                    logger.info("All positions liquidated")
                    break
                time.sleep(1)

            if remaining_positions:
                logger.warning(
                    f"Timeout: {len(remaining_positions)} positions still open"
                )

        except Exception as e:
            logger.error(f"Error during liquidation: {e}")

    def get_buying_power(self) -> float:
        """Broker-agnostic buying power lookup.

        Alpaca exposes it directly on the account object. Interactive
        Brokers doesn't: Lumibot's own IB get_account_summary() only
        requests the '$LEDGER' tag group (for cash/net-liq), which does
        not include 'BuyingPower' — that requires a separate
        reqAccountSummary call with the standard tag name, done here by
        hand since Lumibot doesn't wrap it. Falls back to cash if either
        broker's lookup fails (e.g. during backtests, where there's no
        real broker account to query).
        """
        broker = self.broker

        if hasattr(broker, "api"):
            try:
                return float(broker.api.get_account().buying_power)
            except Exception as e:
                logger.warning(f"Alpaca buying power lookup failed: {e}")

        elif type(broker).__name__ == "InteractiveBrokers" and getattr(broker, "ib", None):
            try:
                ib = broker.ib
                accounts_storage = ib.wrapper.init_accounts()
                reqid = ib.get_reqid()
                ib.reqAccountSummary(reqid, "All", "BuyingPower")
                try:
                    accounts = accounts_storage.get(timeout=ib.max_wait_time)
                except queue.Empty:
                    accounts = None
                ib.cancelAccountSummary(reqid)

                if accounts:
                    bp_rows = [float(a["Value"]) for a in accounts if a["Tag"] == "BuyingPower"]
                    if bp_rows:
                        return bp_rows[0]
            except Exception as e:
                logger.warning(f"Interactive Brokers buying power lookup failed: {e}")

        try:
            return float(self.get_cash())
        except (TypeError, ValueError):
            return 0.0

    def _get_available_capital(self, allocation_percent: float) -> float:
        """Calculate available capital for a strategy.

        Args:
            allocation_percent: Percentage of portfolio to allocate

        Returns:
            Available capital amount
        """
        try:
            portfolio_value = self.get_portfolio_value()
            cash = self.get_cash()
            if portfolio_value is None:
                # 2026-09-04: see _run_pod_strategies' matching comment —
                # a transient broker-balance sync failure leaves this
                # None rather than raising. Previously fell through to
                # the generic except below, logging a confusing
                # "unsupported operand type(s)" error for what's really
                # just a known, harmless skip-this-cycle condition.
                logger.warning("Portfolio value unavailable this cycle (broker sync issue), no capital allocated")
                return 0.0
            min_cash = portfolio_value * config.MIN_CASH_BUFFER

            # Size against buying power (includes margin the broker actually
            # extends), not raw cash — a cash-only cap can under-size or
            # zero-out orders on a small account even when real purchasing
            # power is higher. get_buying_power() falls back to cash on
            # its own if the broker lookup fails.
            buying_power = self.get_buying_power()

            # 2026-09-09: sanity check, independent of HARD_POSITION_
            # CEILING_GBP below — that bounds the OUTPUT, this catches
            # the INPUT anomaly itself (IB reporting an implausible
            # figure) and makes it loud the same cycle it happens. See
            # config.CAPITAL_SANITY_THRESHOLD_GBP's own comment and
            # project_100k_tsla_incident_and_hard_ceiling_fix memory —
            # this exact class of anomaly produced a real $100,271 fill
            # on 2026-09-08 and went undetected for a full day.
            for label, value in (("buying_power", buying_power), ("portfolio_value", portfolio_value)):
                if value is not None and value > config.CAPITAL_SANITY_THRESHOLD_GBP:
                    anomaly = {
                        "timestamp": self.get_datetime().isoformat(),
                        "field": label,
                        "value": value,
                        "threshold": config.CAPITAL_SANITY_THRESHOLD_GBP,
                    }
                    self.capital_anomalies.append(anomaly)
                    logger.critical(
                        f"CAPITAL SANITY CHECK FAILED: {label}={value:,.2f} exceeds "
                        f"config.CAPITAL_SANITY_THRESHOLD_GBP ({config.CAPITAL_SANITY_THRESHOLD_GBP:,.2f}) — "
                        f"IB may be reporting an implausible figure (see "
                        f"project_100k_tsla_incident_and_hard_ceiling_fix memory). Position sizing is "
                        f"still bounded by config.HARD_POSITION_CEILING_GBP, but this needs human review."
                    )

            allocated = portfolio_value * allocation_percent
            # Cap by buying power, still keeping the cash safety buffer —
            # min_cash is subtracted from buying power too so a leveraged
            # order never gets sized as if that cash cushion didn't exist.
            # Also subtract capital already committed by other strategies
            # earlier this same iteration (see _cycle_reservations) — their
            # orders likely haven't filled yet, so buying_power alone would
            # still look untouched.
            reserved = sum(self._cycle_reservations.values())
            available = min(allocated, buying_power - min_cash - reserved)
            # 2026-09-09: hard ceiling, independent of whatever IB
            # reports for buying_power/portfolio_value this cycle — see
            # config.HARD_POSITION_CEILING_GBP's own comment for the two
            # real live incidents (2026-09-03, 2026-09-08 — a real
            # $100,271 TSLA fill) this specifically guards against. A
            # percentage-of-portfolio cap alone doesn't help when the
            # portfolio figure itself is briefly wrong.
            available = min(available, config.HARD_POSITION_CEILING_GBP)
            return max(0, available)

        except Exception as e:
            logger.error(f"Error calculating available capital: {e}")
            return 0.0

    def start_trading(self):
        """Start the trading bot."""
        self.is_trading = True
        self.shutdown_requested = False
        # Anchors watchdog.sh's "no iteration completed yet" grace period
        # to WHEN TRADING ACTUALLY STARTED, not process/tmux-session boot
        # time. Found live 2026-09-03: a bot idle (is_trading=False) for
        # several minutes before Start was clicked already had a session
        # age past STARTUP_GRACE_SECONDS, so watchdog restarted it only
        # ~20s after Start — long before the next iteration was even due
        # (Lumibot's scheduler runs on its own fixed ~60s cadence, not
        # triggered by is_trading flipping). See /api/status's
        # trading_started_at field.
        self.trading_started_at = datetime.now()
        logger.info("Trading started")

    def stop_trading(self):
        """Stop the trading bot and liquidate positions."""
        self.shutdown_requested = True
        logger.info("Shutdown requested")

    def get_performance_metrics(self) -> Dict:
        """Get performance metrics for both strategies."""
        return {
            "is_trading": self.is_trading,
            "portfolio_value": self.get_portfolio_value(),
            "cash": self.get_cash(),
            "positions": len(self.get_positions()),
            "open_bracket_orders": len(self.bracket_orders),
            "closed_positions": len(self.closed_positions),
            "strategy_performance": self.strategy_performance,
            "timestamp": datetime.now().isoformat(),
        }
