"""Portfolio Manager (2026-09-03) — cross-category capital allocation,
the piece that sits between the five scan/filter/regime pods
(stock_scanner.py, etf_scanner.py, commodity_scanner.py, forex_scanner.py,
crypto_scanner.py) and actual order placement.

Deliberately a NEW, ISOLATED module, not wired into the live trading
path. Take-profit/stop-loss rules and real order handoff are the next
two pieces after this one, not part of this module's scope.

Why this exists, not just config.MAX_POSITION_SIZE reused as-is
------------------------------------------------------------------
strategy_manager.py's _get_available_capital() already does per-position
sizing (config.MAX_POSITION_SIZE = 0.10, i.e. 10% of portfolio value per
position, capped by buying power minus a config.MIN_CASH_BUFFER = 0.05
cash reserve minus whatever's already reserved this cycle). That formula
runs against ONE undifferentiated capital pool shared by all 8 live
strategies. Found live 2026-09-03: on a freshly-restarted, completely
flat bot, the very first signal of the session (breakout on TSLA) claimed
a full 10% slice — a real $99k+ position — simply because nothing else
had reserved capital yet that cycle. That's a first-come-first-served
outcome, not a designed one: a quiet cycle hands one strategy the whole
slice, a busy cycle splits it many ways, with no relationship to how
many categories or candidates actually exist.

This module fixes that by budgeting capital PER CATEGORY first (so
Forex having a loud cycle can't crowd out Stock, Commodity, ETF, or
Crypto), then splitting each category's budget evenly across however
many actionable (BUY-biased) candidates that category produced THIS
cycle (so five simultaneous Stock signals don't each try to claim the
same 10% independently), with the existing single-position 10%-of-
portfolio cap still applied on top as an absolute ceiling either way.
The result is bounded, predictable exposure per category and per
position, not a race to see which signal gets evaluated first.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import config
try:
    # 2026-09-10: IB-only, archived to ib_legacy/ (Alpaca-only move) --
    # only used as a type hint on the unused get_investable_capital()
    # below (pod-era code, never actually called in the live path).
    from ib_connector import IBConnector
except ImportError:
    IBConnector = None

logger = logging.getLogger(__name__)

# Categories match the five pods' own domains (stock_scanner.py,
# etf_scanner.py, commodity_scanner.py, forex_scanner.py,
# crypto_scanner.py) — plain strings, not ib_connector.AssetClass,
# since a category here is "which pod produced this candidate," not
# an IB contract type (Commodity's own instruments ARE AssetClass.ETF
# under the hood, per commodity_scanner.py's own docstring — the pods
# are deliberately separate for load-isolation, not because the
# underlying contract type differs).
CATEGORIES = ("STOCK", "ETF", "COMMODITY", "FOREX", "CRYPTO")

# Equal split by default — five pods built specifically so no one
# category can overload/crowd another (explicit user direction), so an
# equal weight is the natural default. Override per-category here if the
# user later wants e.g. Forex/Crypto held to a smaller slice than Stock —
# documented as a deliberate, easy-to-revisit choice, not load-bearing
# math.
CATEGORY_WEIGHTS = {c: 1.0 / len(CATEGORIES) for c in CATEGORIES}

# Same numbers config.py already uses for the live 8 strategies' single-
# pool sizing — reused, not re-derived, so this module's absolute ceiling
# matches the risk tolerance already in production rather than
# introducing a second, different number.
MAX_POSITION_PCT_OF_PORTFOLIO = 0.10  # matches config.MAX_POSITION_SIZE
CASH_BUFFER_PERCENT = 0.05            # matches config.MIN_CASH_BUFFER

# 2026-09-09: hard, IB-independent ceiling on any single position's
# dollar_amount — now config.HARD_POSITION_CEILING_GBP, shared with
# strategy_manager.py's _get_available_capital (the 8 core strategies)
# so both capital-sizing paths use the exact same number. See that
# constant's own comment in config.py for the two real live incidents
# (2026-09-03 -- this module's own docstring above; 2026-09-08 -- a
# real, FILLED 279-share TSLA order at $359.38 = $100,271.02, sized off
# a category_budget of $777,021.88) this specifically guards against.


@dataclass
class CandidateSignal:
    """What a caller builds from each pod's scan results before handing
    them to allocate() — deliberately NOT importing each pod's own
    Result dataclass here, so this module doesn't need to know their
    exact shapes (they already differ slightly pod to pod: e.g. only
    some carry adx). A caller filters each pod's scan_universe() output
    down to bias == "BUY" and wraps each survivor in one of these."""
    symbol: str
    category: str  # one of CATEGORIES
    regime: Optional[str] = None


@dataclass
class Allocation:
    symbol: str
    category: str
    dollar_amount: float
    category_budget: float
    category_candidate_count: int
    capped_by: str  # "category_split" or "position_ceiling" — which limit actually bound


def _validate_category(category: str):
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category {category!r} — must be one of {CATEGORIES}")


# 2026-09-04: replaces ib_side_channel_trader.py's old flat
# MAX_CONCURRENT_INTERNATIONAL_POSITIONS = 5 constant — explicit user
# direction: a risk cap on total concurrent aux-owned positions
# (international stocks/ETF/commodity/forex/crypto, all sharing one
# pool) shouldn't be a fixed number picked once and forgotten. A
# healthy, well-capitalized account can responsibly carry more
# simultaneous positions; a depleted one should carry fewer — the cap
# should track the account's OWN real capacity, not a number chosen
# back when the aux channel only ever held plain international stocks.
# Bounded both ends: MIN_CONCURRENT_POSITIONS keeps the aux channel
# usable even on a small/newly-drawn-down account, MAX_CONCURRENT_
# POSITIONS_CEILING is an operational ceiling independent of capital —
# ib_side_channel_trader.py's check_exits() re-prices every open aux
# position SEQUENTIALLY every cycle (a real, already-hit stall cause
# earlier today), so slot count can't scale with capital alone without
# risking that same class of stall again as the account grows.
MIN_CONCURRENT_POSITIONS = 3
MAX_CONCURRENT_POSITIONS_CEILING = 15


def max_concurrent_positions(portfolio_value: float, investable_capital: float) -> int:
    """How many concurrent aux-owned positions (international stock/
    ETF/commodity/forex/crypto combined) the account can responsibly
    support right now, given ITS OWN real capital — not a fixed number.
    One "full" position costs up to MAX_POSITION_PCT_OF_PORTFOLIO of
    portfolio_value (the same per-position ceiling `allocate()` already
    enforces), so how many of those the currently investable capital
    could actually fund is the natural, self-scaling slot count.
    Recomputed live each time it's called — a losing streak that
    shrinks portfolio_value tightens the cap on its own, a winning one
    loosens it, with no manual retuning needed either way."""
    if portfolio_value <= 0:
        return MIN_CONCURRENT_POSITIONS
    position_ceiling = portfolio_value * MAX_POSITION_PCT_OF_PORTFOLIO
    slots = int(investable_capital / position_ceiling) if position_ceiling > 0 else MIN_CONCURRENT_POSITIONS
    return max(MIN_CONCURRENT_POSITIONS, min(slots, MAX_CONCURRENT_POSITIONS_CEILING))


def get_investable_capital(connector: IBConnector) -> float:
    """Total capital available to allocate across all five categories
    this cycle — buying power minus the same cash-reserve percentage
    config.MIN_CASH_BUFFER already applies for the live 8 strategies."""
    buying_power = connector.get_buying_power()
    portfolio_value = connector.get_portfolio_value()
    if buying_power is None or portfolio_value is None:
        raise RuntimeError("Could not fetch buying_power/portfolio_value from IBConnector")
    # 2026-09-09: same sanity check as strategy_manager.py's
    # _get_available_capital -- this exact path (get_investable_capital
    # -> allocate()) is what produced the real $100,271 TSLA fill on
    # 2026-09-08 (see project_100k_tsla_incident_and_hard_ceiling_fix
    # memory). allocate() itself is still bounded by config.
    # HARD_POSITION_CEILING_GBP regardless of this check; this just
    # makes the anomaly loud immediately instead of only bounded.
    for label, value in (("buying_power", buying_power), ("portfolio_value", portfolio_value)):
        if value > config.CAPITAL_SANITY_THRESHOLD_GBP:
            logger.critical(
                f"CAPITAL SANITY CHECK FAILED (pod system): {label}={value:,.2f} exceeds "
                f"config.CAPITAL_SANITY_THRESHOLD_GBP ({config.CAPITAL_SANITY_THRESHOLD_GBP:,.2f}) — "
                f"IB may be reporting an implausible figure. Position sizing is still bounded by "
                f"config.HARD_POSITION_CEILING_GBP, but this needs human review."
            )
    return max(0.0, buying_power - portfolio_value * CASH_BUFFER_PERCENT), portfolio_value


def allocate(candidates: list, investable_capital: float, portfolio_value: float,
              category_weights: dict = None) -> list:
    """candidates: list[CandidateSignal]. Returns list[Allocation], one
    per candidate, in the same order they were given. A category with
    zero candidates this cycle simply allocates nothing (its budget goes
    unused rather than being redistributed — keeping the split
    predictable and easy to reason about, rather than a dynamic
    reallocation scheme that would make one category's allocation depend
    on how many candidates every OTHER category happened to produce)."""
    weights = category_weights or CATEGORY_WEIGHTS
    position_ceiling = portfolio_value * MAX_POSITION_PCT_OF_PORTFOLIO

    by_category = {}
    for c in candidates:
        _validate_category(c.category)
        by_category.setdefault(c.category, []).append(c)

    allocations = []
    for category, members in by_category.items():
        category_budget = investable_capital * weights.get(category, 0.0)
        per_candidate = category_budget / len(members) if members else 0.0
        for c in members:
            # 2026-09-09: config.HARD_POSITION_CEILING_GBP applied last,
            # after the proportional caps above -- see that constant's
            # own comment for why a percentage-of-portfolio cap alone
            # can't protect against IB itself briefly reporting an
            # inflated portfolio_value/buying_power (confirmed live,
            # twice).
            proportional_cap = min(per_candidate, position_ceiling)
            dollar_amount = min(proportional_cap, config.HARD_POSITION_CEILING_GBP)
            if dollar_amount < proportional_cap:
                capped_by = "hard_ceiling"
            elif position_ceiling < per_candidate:
                capped_by = "position_ceiling"
            else:
                capped_by = "category_split"
            allocations.append(Allocation(
                symbol=c.symbol, category=c.category, dollar_amount=round(dollar_amount, 2),
                category_budget=round(category_budget, 2), category_candidate_count=len(members),
                capped_by=capped_by,
            ))
            logger.info(
                f"[portfolio_manager] {c.symbol} ({category}): ${dollar_amount:,.2f} "
                f"(category budget ${category_budget:,.2f} / {len(members)} candidates, "
                f"capped_by={capped_by})"
            )
    return allocations
