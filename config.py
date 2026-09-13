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

# Maximum % of portfolio to allocate per position.
# 2026-09-12: raised 10% -> 36% -> 37% (final value), explicit user
# decision, after a marathon comparison showed the ATR-sizing formula
# (self.risk_pct * pool / stop_distance, see _atr_position_notional in
# extended_strategies_live.py) would otherwise size UP to 100% of the
# account on a tight-stop trade (confirmed live in a backtest: a
# 0.68%-stop PCG trade consumed the entire $136.91 pool) -- ATR sizing has
# no ceiling of its own, it only ever gets capped by whatever this
# constant computes as available_capital. 37% was chosen as a deliberate
# middle ground: well above Kelly's own natural ~26.5% fraction (validated
# on lab_range, the project's best-performing strategy), but nowhere near
# the 100%-of-account concentration ATR sizing hit uncapped.
# IMPORTANT: HARD_POSITION_CEILING_GBP below is a flat DOLLAR amount, not
# a percentage -- once portfolio_value exceeds ~$676 (where 37% of it
# equals $250), the hard ceiling silently takes over as the REAL cap and
# this percentage stops mattering at all. At this account's current size
# (~$136-170) that crossover is moot, but raise HARD_POSITION_CEILING_GBP
# manually if real capital ever grows well past ~$676, or this 37% figure
# becomes decorative. See project memory, "MAX_POSITION_SIZE raised to
# 36/37%" (2026-09-12), for the full smart-money/liquidity-sweep reasoning
# behind not leaving this fully uncapped.
MAX_POSITION_SIZE = 0.37

# Stop loss % from entry price
STOP_LOSS_PERCENT = 0.05  # 5%

# Take profit % from entry price. This is only a fallback for the rare
# case _place_bracket_order runs without a strategy_name (BracketOrder's
# stop_loss_percent/take_profit_percent are None) — normal strategy-driven
# trades always look up signal_logger's DB-backed setting via
# get_strategy_risk() instead (kept in sync manually, see project memory).
# Changed 2026-08-31 from 0.10 to 0.075 (1:1.5 risk:reward, was 1:2) for
# the testing phase — faster-closing trades, more data points sooner.
TAKE_PROFIT_PERCENT = 0.075  # 7.5%

# Maximum number of concurrent positions
MAX_POSITIONS = 10

# Minimum cash buffer (don't invest 100% of portfolio)
MIN_CASH_BUFFER = 0.05  # Keep 5% cash

# 2026-09-09: hard, IB-independent ceiling on any single position's
# dollar amount (in this account's real base currency, GBP — see
# project_ib_currency_and_scanner_findings_2026_09_09 memory), applied
# on TOP of MAX_POSITION_SIZE's percentage cap, in both
# strategy_manager.py's _get_available_capital (the 8 core strategies)
# and portfolio_manager.py's allocate() (the 5-category pod system).
# Found live, TWICE, that a PERCENTAGE-only cap doesn't protect against
# a real incident: 2026-09-03 (portfolio_manager.py's own module
# docstring — a fresh-restart breakout signal claimed a real $99k+ TSLA
# position) and 2026-09-08 (a real, FILLED 279-share TSLA order at
# $359.38 = $100,271.02, sized off a category_budget of $777,021.88).
# Both trace to IB itself briefly reporting an inflated buying_power/
# portfolio_value — MAX_POSITION_SIZE alone can't help, since it's a
# percentage OF that same (possibly wrong) number, so it scales up right
# along with the bug. This is deliberately a small ABSOLUTE number,
# generous enough for this account's real current size (a few hundred
# pounds) but nowhere near large enough to repeat either incident even
# if IB reports another inflated figure. Raise this only as real capital
# genuinely grows, not preemptively.
HARD_POSITION_CEILING_GBP = 250.0

# 2026-09-12 (explicit user request): fixed priority rank, best first,
# for strategy_manager.py's _check_priority_takeovers -- when a symbol is
# already held by a lower-ranked strategy and a higher-ranked one has a
# fresh BUY signal for it this cycle, the higher-ranked strategy takes
# over that position's exit management (stop-loss/take-profit swapped to
# its own; the position itself -- shares, entry price, cost basis -- is
# NEVER touched, no liquidation, no re-entry). See project memory,
# "Priority takeover system" (2026-09-12), for the full trigger rule.
#
# DEFAULT ORDER -- this is a starting point, not a validated ranking: the
# top 15 are ordered by real per-strategy $ contribution from the
# 2026-09-12 week-one (Aug 24-28) shared-pool hierarchy simulation with
# lab_range excluded (project_filter_effect_per_strategy_2026_09_12 /
# project_live_strategies_prior_week_shared_pool_sim_2026_09_12); lab_range
# itself is placed 2nd on the strength of its OWN much larger, separately-
# validated ATR-sized result (+23.71%, see project_sizing_marathon_
# methodology) despite being excluded from that specific sim run. The
# remaining, currently-disabled original 7 strategies are appended in
# their pre-existing sub_strategies dict order (inert while OFF; rank
# among each other only matters if more than one is enabled later).
# EDIT THIS LIST directly to change priority -- no other code change
# needed.
STRATEGY_PRIORITY_RANK = [
    "scalping_v2",
    "lab_range",
    "discount_zone",
    "asymmetric_dual",
    "scalping",
    "volume_divergence_grab",
    "fakeout_breakout_fib",
    "macd_200ema",
    "volume_absorption",
    "candle_taxonomy",
    "supply_demand",
    "ema_ribbon_smi",
    "supertrend_200ema",
    "rigorous_rr",
    "volume_profile",
    "breakout_chandelier",
    "momentum",
    "breakout",
    "mean_reversion",
    "vwap",
    "gap_and_go",
    "reversal",
    "market_profile",
]

# 2026-09-09: separate from HARD_POSITION_CEILING_GBP above — that caps
# the OUTPUT (never size a position past this), this catches the INPUT
# anomaly itself (IB reporting an implausible buying_power/
# portfolio_value) and makes it LOUD immediately, rather than silently
# bounded. Both incidents this same day (see HARD_POSITION_CEILING_GBP's
# comment) went undetected until Group 2's own independent daily agent
# happened to notice the log pattern a day later -- with this, the exact
# moment IB reports something implausible gets logged CRITICAL and
# surfaced on the dashboard (see strategy_bot.capital_anomalies /
# /api/capital-anomalies) the same cycle it happens, not found after
# the fact. Threshold is well above this account's real current size
# (a few hundred pounds) but far below what an oversized position would
# actually need to be flagged as clearly wrong.
CAPITAL_SANITY_THRESHOLD_GBP = 5000.0

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
    "PYPL", "ZM", "INTU", "SNPS", "CDNS",
    "AMD", "MU", "QCOM", "MCHP", "AVGO"
]

# ============================================================================
# NEWS SENTIMENT — two uses, neither is its own trading strategy (2026-09-01,
# see project memory): (1) symbol-selection input for scalping (see
# scalping.py's _sentiment_candidates), and (2) a portfolio-wide emergency
# stop (see strategy_manager.py's _check_news_emergency_stop) — liquidates
# ANY open position, regardless of which strategy opened it, on genuinely
# severe negative news. A per-strategy entry filter (block mean_reversion's
# own entries on bad news) was tested and found REDUNDANT with its
# 5-day-momentum filter (see mean_reversion.py) — sharp price drops and bad
# news are largely the same events in this data, so news doesn't add
# independent entry-filtering value there. It IS independently useful as a
# reactive safety net for positions already open, which momentum can't be
# (momentum is only checked at entry time).
# ============================================================================

# NewsAPI key (get from https://newsapi.org) — unused; news_sentiment.py
# uses Alpaca's own News API instead (see its own module docstring; switched
# from Yahoo Finance scraping 2026-09-01).
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

# SCALPING_SENTIMENT_RELEVANCE_THRESHOLD (renamed 2026-09-01 from
# SENTIMENT_BUY_THRESHOLD — the old name implied a buy decision, but this
# never decides to buy anything; it only decides whether a symbol is
# "relevant enough" for scalping.py to add to its scan candidates. Actual
# entries still go through scalping's own 0.2% price-move trigger). Mean of
# every headline in the NEWS_LOOKBACK_HOURS window must clear this to count
# as relevant — both live (news_sentiment._get_sentiment_score) and the
# historical backtest use this exact same threshold against the exact same
# aggregation (mean of ALL headlines in the window, not a handful of
# recent ones), so the value means the same thing in both places.
SCALPING_SENTIMENT_RELEVANCE_THRESHOLD = 0.6

# Only used by news_sentiment.py's own analyze() for the read-only
# /api/strategy-preview diagnostic (still genuinely frames a BUY/SELL/HOLD
# preview there) — doesn't drive any real trade.
SENTIMENT_SELL_THRESHOLD = 0.3  # Sell if sentiment < 0.3

# News lookback window (hours) — how far back get_alpaca_sentiment looks,
# and the window backtest_scalping_sentiment_historical.py aggregates a
# day's headlines over.
NEWS_LOOKBACK_HOURS = 24

# Much more extreme than SCALPING_SENTIMENT_RELEVANCE_THRESHOLD or
# SENTIMENT_SELL_THRESHOLD on purpose — this triggers an immediate
# liquidation of a REAL open position, so it should only fire on genuinely
# severe negative news, not routine bad-press noise. Checked against a
# short (NEWS_EMERGENCY_STOP_LOOKBACK_HOURS) recent window, not the full
# 24h NEWS_LOOKBACK_HOURS, so it reacts to what's fresh rather than
# something already priced in hours ago.
NEWS_EMERGENCY_STOP_THRESHOLD = -0.7
NEWS_EMERGENCY_STOP_LOOKBACK_HOURS = 6

# Found live 2026-09-01: fired on TSLA off just 3 articles, none of them
# genuinely severe TSLA-specific news (a NIO-outlook piece, a "competitor
# beating Tesla" comparison, and a mixed/ambiguous quote a plain classifier
# misread as strongly negative) — averaging that few headlines is too
# noisy to trust for a mechanism that triggers a real forced liquidation.
# Below this many articles in the lookback window, the score is treated as
# neutral (insufficient evidence) rather than acted on either way.
NEWS_EMERGENCY_STOP_MIN_ARTICLES = 5

# Pool of symbols scalping checks for a current positive sentiment spike
# (see scalping.py's _sentiment_candidates). SPY/QQQ/IWM removed 2026-09-02
# — this IB account can't buy them at all (KID/PRIIPs restriction, see
# symbol_universe.py's KID_RESTRICTED_SYMBOLS for the full explanation),
# so surfacing them here as sentiment-driven scan candidates was pure
# waste — a sentiment spike could never actually convert to a real trade.
NEWS_SENTIMENT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "TSLA", "META"
]

# Optional asset-class "second opinion" riders (2026-09-05, see
# asset_class_riders.py's module docstring for the real intermarket
# mechanism behind each one). Non-compulsory by design — each fails open
# (confirmed=True) on any data problem, so these can never stall real
# order flow, the same fail-open philosophy as meta_model's confidence
# gate. VIX applies to all 8 live strategies (they only trade individual
# US equities per symbol_universe.py's KID_RESTRICTED_SYMBOLS); gold-macro
# and crypto-rotation apply to the COMMODITY/CRYPTO pods respectively
# (_run_pod_strategies, off by default, not yet live-tested end-to-end).
ENABLE_VIX_RIDER = True
VIX_HIGH_THRESHOLD = 25.0
VIX_SPIKE_LOOKBACK_DAYS = 5
VIX_SPIKE_MAX_INCREASE_PCT = 0.15

ENABLE_GOLD_MACRO_RIDER = True
GOLD_MACRO_DXY_LOOKBACK_DAYS = 10

ENABLE_CRYPTO_ROTATION_RIDER = True
CRYPTO_ROTATION_LOOKBACK_DAYS = 5

# Stub only (see check_forex_carry_rider's docstring) — kept off so it's
# never mistaken for a working gate. Turning it on changes nothing yet.
ENABLE_FOREX_CARRY_RIDER = False

# ============================================================================
# INTERNATIONAL MARKETS (2026-09-02) — IB scanner-driven expansion beyond
# US equities, so the bot has something to trade when the US market is
# closed. Only markets confirmed to have FREE (no paid subscription)
# data access on this account are listed here — verified directly via
# ibapi against live IB Gateway, not assumed. Location/scan codes pulled
# from IB's own reqScannerParameters() response, not guessed.
#
# 2026-09-02 note (now stale, corrected 2026-09-04): this comment used
# to cite a "project_ib_market_data_audit" memory claiming Japan,
# Germany, and Canada were tested and found NOT free, deliberately
# excluded. That memory file no longer exists (or never did — could not
# be found when checked 2026-09-04), so its exact test methodology
# can't be cross-checked. A fresh, careful live re-test of Japan
# specifically on 2026-09-04 (reqMktData with reqMarketDataType(3)'s
# delayed fallback + reqHistoricalData, both against a real TSEJ
# contract) returned real delayed quotes (standard 10167 notice, same
# pattern already handled everywhere else in this project) and real
# historical daily bars — zero paid-subscription errors. Either this
# account's entitlements changed since whatever the original test was,
# or that test never called reqMarketDataType(3) and misdiagnosed a
# missing-delayed-fallback timeout as "not free" — either way, Japan is
# now added below (see "japan" entry) based on verified-live evidence,
# superseding the old exclusion. Germany and Canada were NOT re-tested
# and remain excluded — this correction is Japan-specific, not a
# blanket "the old audit was wrong."
# NOTE: IB's live reqScannerSubscription() ("find me the big movers")
# was tested and requires a PAID real-time market data subscription per
# market (error 492) even though plain delayed quotes/historical bars
# for these same markets are free — a genuine account-level IBKR limit,
# not a code problem. Rather than gate this whole feature on a paid
# subscription, "big movers" is instead computed the same way this
# codebase already does it for US stocks (momentum_allocator.py): rank
# the market's FULL broad index universe by recent price change using
# the free delayed historical bars that ARE confirmed working.
#
# 2026-09-02: broadened from an initial ~10-name hand-picked watchlist
# to the full index constituent universe per the user's explicit
# direction — a strategy shouldn't be limited to a curated symbol list,
# it should see the whole broad market and only specific symbols get
# EXCLUDED once there's real evidence a strategy doesn't work on them
# (same philosophy as mean_reversion's DIA exclusion on the US side —
# see project_mean_reversion_future_forex memory). "universe" below is
# the FTSE 100 / ASX 200 constituent list (pulled 2026-09-02, will drift
# as index composition changes over time — not auto-updated).
INTERNATIONAL_MARKETS = {
    "uk": {
        "exchange": "LSE",
        "currency": "GBP",
        "timezone": "Europe/London",
        "open_time": "08:00",
        "close_time": "16:30",
        # 2026-09-04: "sessions" is the real list of (open, close)
        # windows this market trades in a day, checked by is_market_
        # open/is_tradeable. LSE trades one continuous session — added
        # for schema uniformity with Japan below (which needs two),
        # not because LSE itself has a gap. open_time/close_time above
        # are kept too (first session's open / last session's close) —
        # still used as-is by the Friday-close-buffer logic
        # (_minutes_until_close), unaffected by this.
        "sessions": [("08:00", "16:30")],
        "universe": [
            "III", "ABDN", "ADM", "AAF", "ALW", "AAL", "ANTO", "ABF", "AZN", "AUTO",
            "AV", "BAB", "BA", "BARC", "BTRW", "BEZ", "BP", "BATS", "BLND", "BT.A",
            "BNZL", "BRBY", "CNA", "CCEP", "CCH", "CPG", "CCC", "CTEC", "CRDA", "DCC",
            "DGE", "DPLM", "EDV", "ENT", "EXPN", "FCIT", "FRES", "GAW", "GLEN", "GSK",
            "HLN", "HLMA", "HSX", "HWDN", "HSBA", "ICG", "IGG", "IHG", "IMI", "IMB",
            "INF", "IAG", "ITRK", "INVP", "JD", "BGEO", "KGF", "LAND", "LGEN", "LLOY",
            "LMP", "LSEG", "MNG", "MKS", "MRO", "MTLN", "NG", "NWG", "NXT", "PSON",
            "PSH", "PSN", "PCT", "PRU", "RKT", "REL", "RTO", "RIO", "RR", "SGE",
            "SBRY", "SDR", "SMT", "SGRO", "SVT", "SHEL", "SMIN", "SN", "SPX", "SSE",
            "STAN", "SDLF", "STJ", "TSCO", "BBOX", "ULVR", "UU", "VOD", "WEIR", "WTB",
        ],
    },
    "australia": {
        "exchange": "ASX",
        "currency": "AUD",
        "timezone": "Australia/Sydney",
        "open_time": "10:00",
        "close_time": "16:00",
        # 2026-09-04: see "uk" entry's comment on "sessions" — same
        # reasoning, ASX also trades one continuous session.
        "sessions": [("10:00", "16:00")],
        "universe": [
            "360", "4DX", "A2M", "AAI", "AFI", "AGL", "AIA", "ALD", "ALK", "ALL",
            "ALQ", "ALX", "AMC", "AMP", "ANN", "ANZ", "APA", "APE", "ARB", "ARG",
            "ASB", "ASK", "ASX", "AUB", "AZJ", "BEN", "BFL", "BGA", "BGL", "BHP",
            "BOQ", "BPT", "BRG", "BSL", "BWP", "BXB", "CAR", "CBA", "CDA", "CEN",
            "CGF", "CHC", "CIA", "CIP", "CLW", "CMM", "CNU", "COH", "COL", "CPU",
            "CQR", "CSC", "CSL", "CTD", "CWY", "CYL", "DBI", "DNL", "DOW", "DRO",
            "DRR", "DXS", "DYL", "EBO", "EDV", "EMR", "EOS", "EVN", "EVT", "FBU",
            "FLT", "FMG", "FPH", "FRW", "GDG", "GGP", "GMD", "GMG", "GNE", "GPT",
            "GQG", "HDN", "HUB", "HVN", "IAG", "IFL", "IFT", "IGO", "ILU", "IMD",
            "JBH", "JHX", "L1G", "LLC", "LNW", "LOV", "LSF", "LTR", "LYC", "MCY",
            "MEZ", "MFF", "MFG", "MGR", "MIN", "MND", "MPL", "MQG", "MSB", "MTS",
            "MXT", "NAB", "NEM", "NHC", "NHF", "NIC", "NSR", "NST", "NWH", "NWL",
            "NWS", "NXG", "NXT", "OBM", "ORA", "ORG", "ORI", "PDI", "PDN", "PLS",
            "PME", "PMV", "PNI", "PPT", "PRN", "PRU", "PXA", "QAN", "QBE", "QUB",
            "RDX", "REA", "REG", "REH", "RGN", "RHC", "RIO", "RMD", "RMS", "RRL",
            "RSG", "RWC", "RYM", "S32", "SCG", "SDF", "SEK", "SFR", "SGH", "SGM",
            "SGP", "SHL", "SIG", "SMR", "SNZ", "SOL", "SPK", "STO", "SUL", "SUN",
            "TAH", "TCL", "TLC", "TLS", "TLX", "TNE", "TPG", "TUA", "TWE", "VAU",
            "VCX", "VEA", "VGN", "VNT", "WAF", "WAM", "WBC", "WDS", "WES", "WGX",
            "WHC", "WLE", "WOR", "WOW", "WTC", "XRO", "YAL", "ZIM", "ZIP",
        ],
    },
    "japan": {
        # 2026-09-04: added because USD/JPY already exists in FOREX_PAIRS
        # below, so real live currency conversion (ib_connector.
        # convert_currency) already works for JPY with no new pair
        # needed — unlike Hong Kong (HKD) or Singapore (SGD), which
        # would need one added first. IB's real exchange code for TSE
        # is "TSEJ" (confirmed live via reqContractDetails, not
        # assumed), and Japanese equities trade under numeric IB
        # symbols (e.g. "7203" = Toyota), not letter tickers like UK/
        # Australia — every symbol below was individually confirmed to
        # resolve via reqContractDetails before being included, same
        # discipline used for the LSEETF tickers built earlier the same
        # day.
        #
        # FIXED 2026-09-04 (same day as the gap was first found): TSE
        # has a real intraday lunch closure — confirmed live via
        # reqContractDetails' own liquidHours field: 09:00-11:30 and
        # 12:30-15:30 JST, not one continuous session. "sessions" below
        # (added the same day to ib_connector.is_market_open and
        # ib_side_channel_trader.py's own _market_is_open, both now
        # iterate the list instead of one open/close pair) makes
        # is_market_open/is_tradeable correctly report closed during
        # the 11:30-12:30 gap instead of a false "open" that would have
        # sent a doomed order attempt to IB (the same failure class
        # already fixed once the same day for LSE — a real ~32s round
        # trip before IB cancels it, contributing to two watchdog
        # stalls). open_time/close_time below are kept as first-open/
        # last-close for the Friday-close-buffer logic
        # (_minutes_until_close), which only cares about the day's
        # final close, not the lunch gap.
        "exchange": "TSEJ",
        "currency": "JPY",
        "timezone": "Asia/Tokyo",
        "open_time": "09:00",
        "close_time": "15:30",
        "sessions": [("09:00", "11:30"), ("12:30", "15:30")],
        "universe": [
            "7203", "6758", "9984", "6501", "8306", "6861", "7267", "9432", "4063", "6098",
            "8035", "6902", "6503", "7751", "6702", "4519", "9433", "8058", "8031", "6367",
            "9983", "4568", "6273", "7974", "8801", "8316", "4661", "6178", "9020", "5108",
        ],
    },
    "hong_kong": {
        # 2026-09-04: same correction as Japan — the old "not free"
        # exclusion this file used to cite covered Germany/Canada, and
        # Hong Kong/Singapore were never tested at all before today.
        # Fresh live verification (reqMktData w/ reqMarketDataType(3)
        # delayed fallback + reqHistoricalData) against real SEHK
        # contracts returned real delayed quotes (10167 notice, same as
        # everywhere else) and real historical bars, zero paid-
        # subscription errors. IB exchange code "SEHK", numeric symbols
        # without leading zeros (e.g. "5" = HSBC, "700" = Tencent, not
        # "0005"/"0700") — confirmed live via reqContractDetails, "0005"
        # itself does NOT resolve. Real intraday lunch closure
        # (confirmed live via liquidHours): 09:30-12:00, 13:00-16:10 HKT
        # — two sessions, same shape as Japan.
        #
        # CURRENCY CONVERSION GAP: HKD has no entry in FOREX_PAIRS below
        # — ib_connector.convert_currency has no live rate source for
        # USD<->HKD, so this market works for the plain international-
        # equities scanner (run_international_and_forex_cycle, which
        # pre-dates and doesn't depend on pod-pipeline currency
        # conversion) but NOT for ETF/Commodity-pod-style ordering of a
        # hypothetical HKD-denominated instrument until a USD/HKD pair
        # is added — a separate task, out of scope here.
        "exchange": "SEHK",
        "currency": "HKD",
        "timezone": "Asia/Hong_Kong",
        "open_time": "09:30",
        "close_time": "16:10",
        "sessions": [("09:30", "12:00"), ("13:00", "16:10")],
        "universe": [
            "5", "700", "941", "1299", "3690", "9988", "2318", "388", "883", "1398",
            "175", "1211", "2020", "3988", "6862",
        ],
    },
    "singapore": {
        # 2026-09-04: see "hong_kong" entry's comment — same discovery
        # process, never previously tested. IB exchange code "SGX".
        # Real intraday lunch closure confirmed live via liquidHours:
        # 09:00-12:00, 13:00-17:16 (IB reports this market's timeZoneId
        # as "Hongkong" — same UTC+8 offset as Singapore, used
        # Asia/Singapore below for the correct zoneinfo name instead).
        #
        # CURRENCY CONVERSION GAP: same as Hong Kong — SGD has no
        # FOREX_PAIRS entry, plain international-equities scanner only
        # until a USD/SGD pair is added.
        "exchange": "SGX",
        "currency": "SGD",
        "timezone": "Asia/Singapore",
        "open_time": "09:00",
        "close_time": "17:16",
        "sessions": [("09:00", "12:00"), ("13:00", "17:16")],
        "universe": [
            "D05", "O39", "U11", "C6L", "Z74", "C38U", "A17U", "BN4", "G13", "S68",
            "S63", "F34", "C09", "N2IU", "V03",
        ],
    },
    "germany": {
        # 2026-09-04: see "hong_kong" entry's comment on the discovery
        # process — this one specifically corrects the old "Germany
        # tested, not free" claim this file used to cite (unverifiable,
        # the memory it referenced doesn't exist). Fresh live
        # verification: real delayed quotes + real historical bars for
        # SAP on IB's "IBIS" exchange code (XETRA), zero paid-
        # subscription errors. ONE continuous session confirmed via
        # liquidHours (09:00-17:45 MET) — no lunch-break quirk here,
        # unlike the three Asian markets above.
        #
        # EUR already has a working forex pair (EUR/USD, FOREX_PAIRS
        # below) — Germany is the one addition today that works for
        # BOTH the plain international scanner AND ETF/Commodity-pod-
        # style ordering with no new forex pair needed, same situation
        # Japan/JPY was in.
        "exchange": "IBIS",
        "currency": "EUR",
        "timezone": "Europe/Berlin",
        "open_time": "09:00",
        "close_time": "17:45",
        "sessions": [("09:00", "17:45")],
        "universe": [
            "SAP", "SIE", "ALV", "DTE", "AIR", "BAS", "BAYN", "BMW", "VOW3", "DBK",
            "MBG", "MUV2", "IFX", "RWE", "ADS",
        ],
    },
    "canada": {
        # 2026-09-04: see "hong_kong"/"germany" entries' comments — this
        # one corrects the old "Canada tested, not free" claim. Fresh
        # live verification: real delayed quotes + real historical bars
        # for RY (Royal Bank of Canada) on IB's "TSE" exchange code
        # (Toronto Stock Exchange — NOT to be confused with "TSEJ",
        # Tokyo, used by the "japan" entry above; IB's plain "TSE" code
        # means Toronto). ONE continuous session confirmed via
        # liquidHours (09:30-16:00 US/Eastern — Toronto runs on the same
        # clock as US markets).
        #
        # CURRENCY CONVERSION now WORKS (2026-09-04, same day as the
        # comprehensive market sweep below): USD/CAD confirmed live via
        # the newer ibapi_forex client (protocol 163+, same situation
        # as EUR/USD/GBP/USD) — the gap this comment used to describe
        # is closed, ETF/Commodity-pod-style ordering now works for CAD.
        "exchange": "TSE",
        "currency": "CAD",
        "timezone": "US/Eastern",
        "open_time": "09:30",
        "close_time": "16:00",
        "sessions": [("09:30", "16:00")],
        "universe": [
            "RY", "TD", "SHOP", "ENB", "CNR", "BNS", "BMO", "CP", "TRI", "SU",
            "CM", "MFC", "BCE", "GIB.A", "ATD",
        ],
    },
    "mexico": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/MXN pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "MEXI",
        "currency": "MXN",
        "timezone": "America/Mexico_City",
        "open_time": "08:30",
        "close_time": "15:00",
        "sessions": [("08:30", "15:00")],
        "universe": [
            "WALMEX", "GFNORTEO", "FEMSAUBD", "GMEXICOB", "CEMEXCPO", "BIMBOA", "ALFAA", "KIMBERA",
        ],
    },
    "brazil": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # CURRENCY CONVERSION GAP: no direct USD/BRL pair exists on IB at all
        # (tested on both the plain and newer ibapi_forex clients -- genuine
        # 'No security definition' on both, not a version issue; likely a real
        # currency-control restriction on offshore retail FX for this
        # currency). Plain international-equities scanner only until/unless IB
        # ever offers this pair.
        "exchange": "B3",
        "currency": "BRL",
        "timezone": "America/Sao_Paulo",
        "open_time": "09:45",
        "close_time": "18:00",
        "sessions": [("09:45", "18:00")],
        "universe": [
            "PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3", "B3SA3", "WEGE3", "RENT3", "SUZB3", "BBAS3",
            "GGBR4",
        ],
    },
    "austria": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "VSE",
        "currency": "EUR",
        "timezone": "Europe/Vienna",
        "open_time": "09:00",
        "close_time": "17:45",
        "sessions": [("09:00", "17:45")],
        "universe": [
            "OMV", "EVN", "VOE", "ANDR", "VIG", "RBI", "WIE", "POST", "VER",
        ],
    },
    "france": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "SBF",
        "currency": "EUR",
        "timezone": "Europe/Paris",
        "open_time": "09:00",
        "close_time": "17:40",
        "sessions": [("09:00", "17:40")],
        "universe": [
            "MC", "OR", "BNP", "TTE", "AI", "SU", "DG", "CS", "BN", "KER",
            "SGO",
        ],
    },
    "italy": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "BVME",
        "currency": "EUR",
        "timezone": "Europe/Rome",
        "open_time": "09:00",
        "close_time": "17:30",
        "sessions": [("09:00", "17:30")],
        "universe": [
            "ENI", "ISP", "ENEL", "UCG", "G", "RACE", "TIT", "PST", "MB",
        ],
    },
    "netherlands": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "AEB",
        "currency": "EUR",
        "timezone": "Europe/Amsterdam",
        "open_time": "09:00",
        "close_time": "17:40",
        "sessions": [("09:00", "17:40")],
        "universe": [
            "ASML", "INGA", "PHIA", "AD", "HEIA", "AKZA", "RAND", "WKL",
        ],
    },
    "spain": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "BM",
        "currency": "EUR",
        "timezone": "Europe/Madrid",
        "open_time": "09:00",
        "close_time": "17:30",
        "sessions": [("09:00", "17:30")],
        "universe": [
            "SAN", "ITX", "IBE", "BBVA", "TEF", "REP", "FER", "AMS", "CABK", "ACS",
        ],
    },
    "switzerland": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/CHF pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "EBS",
        "currency": "CHF",
        "timezone": "Europe/Zurich",
        "open_time": "09:00",
        "close_time": "17:20",
        "sessions": [("09:00", "17:20")],
        "universe": [
            "NESN", "NOVN", "UBSG", "ZURN", "ABBN", "CFR", "SIKA", "LONN", "GIVN",
        ],
    },
    "czech": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/CZK pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "PRA",
        "currency": "CZK",
        "timezone": "Europe/Prague",
        "open_time": "09:00",
        "close_time": "16:30",
        "sessions": [("09:00", "16:30")],
        "universe": [
            "CEZ", "KOMB", "MONET", "VIG",
        ],
    },
    "denmark": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/DKK pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "CPH",
        "currency": "DKK",
        "timezone": "Europe/Copenhagen",
        "open_time": "09:00",
        "close_time": "17:30",
        "sessions": [("09:00", "17:30")],
        "universe": [
            "DANSKE", "ORSTED", "CARL.B", "DSV", "GN", "AMBU.B", "COLO.B", "TRYG",
        ],
    },
    "norway": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/NOK pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "OSE",
        "currency": "NOK",
        "timezone": "Europe/Oslo",
        "open_time": "09:00",
        "close_time": "17:30",
        "sessions": [("09:00", "17:30")],
        "universe": [
            "EQNR", "DNB", "NHY", "MOWI", "TEL", "YAR", "ORK", "GJF", "SALM",
        ],
    },
    "portugal": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "BVL",
        "currency": "EUR",
        "timezone": "Europe/Lisbon",
        "open_time": "08:00",
        "close_time": "16:30",
        "sessions": [("08:00", "16:30")],
        "universe": [
            "EDP", "GALP", "JMT", "BCP", "EDPR", "NOS", "SON",
        ],
    },
    "romania": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/RON pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "BVB",
        "currency": "RON",
        "timezone": "Europe/Bucharest",
        "open_time": "09:00",
        "close_time": "17:00",
        "sessions": [("09:00", "17:00")],
        "universe": [
            "SNP", "TLV", "BRD", "FP", "EL", "H2O",
        ],
    },
    # Russia (MOEX) deliberately excluded (2026-09-04) — a live technical
    # check found real quote/historical/forex data during the 2026-09-04
    # market sweep, but that turned out to be stale/non-representative:
    # IB's own current sanctions disclosures state clients cannot open or
    # close MOEX positions and IB does not currently receive MOEX pricing
    # data at all, and IB was fined $11.8M by OFAC in July 2025 for sanctions
    # violations in exactly this area. Confirmed via web research, not just
    # the API check — user explicit direction to leave this out entirely.
    # Do not re-add without re-verifying the real sanctions/compliance
    # status directly, not just a technical data-access check.
    "slovenia": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: EUR already has a working pair (EUR/USD,
        # existing FOREX_PAIRS entry) -- works for ETF/Commodity-pod-style
        # ordering with no new pair needed.
        "exchange": "LJSE",
        "currency": "EUR",
        "timezone": "Europe/Ljubljana",
        "open_time": "09:15",
        "close_time": "15:30",
        "sessions": [("09:15", "15:30")],
        "universe": [
            "KRKG", "POSR", "TLSG",
        ],
    },
    "israel": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/ILS pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "TASE",
        "currency": "ILS",
        "timezone": "Asia/Jerusalem",
        "open_time": "09:59",
        "close_time": "13:50",
        "sessions": [("09:59", "13:50")],
        "universe": [
            "TEVA", "ICL", "ESLT", "POLI", "LUMI", "NICE",
        ],
    },
    "saudi_arabia": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/SAR pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "TADAWUL",
        "currency": "SAR",
        "timezone": "Asia/Riyadh",
        "open_time": "10:00",
        "close_time": "15:20",
        "sessions": [("10:00", "15:20")],
        "universe": [
            "2222", "1120", "2010", "1180", "1211", "7010", "2350",
        ],
    },
    "uae_adx": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/AED pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "ADX",
        "currency": "AED",
        "timezone": "Asia/Dubai",
        "open_time": "10:00",
        "close_time": "14:45",
        "sessions": [("10:00", "14:45")],
        "universe": [
            "FAB", "ADNOCDIST", "ADNOCGAS", "ALDAR",
        ],
    },
    "uae_dfm": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/AED pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "DFM",
        "currency": "AED",
        "timezone": "Asia/Dubai",
        "open_time": "10:00",
        "close_time": "14:45",
        "sessions": [("10:00", "14:45")],
        "universe": [
            "EMAAR", "DIB", "DEWA", "DFM",
        ],
    },
    "south_korea": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/KRW pair confirmed live (plain ibapi
        # client), works for ETF/Commodity-pod-style ordering.
        "exchange": "KRX",
        "currency": "KRW",
        "timezone": "Asia/Seoul",
        "open_time": "09:00",
        "close_time": "15:30",
        "sessions": [("09:00", "15:30")],
        "universe": [
            "005930", "000660", "373220", "207940", "005380", "006400", "035420", "051910",
        ],
    },
    "india": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # CURRENCY CONVERSION GAP: no direct USD/INR pair exists on IB at all
        # (tested on both the plain and newer ibapi_forex clients -- genuine
        # 'No security definition' on both, not a version issue; likely a real
        # currency-control restriction on offshore retail FX for this
        # currency). Plain international-equities scanner only until/unless IB
        # ever offers this pair.
        "exchange": "NSE",
        "currency": "INR",
        "timezone": "Asia/Kolkata",
        "open_time": "09:15",
        "close_time": "15:30",
        "sessions": [("09:15", "15:15"), ("15:20", "15:30")],
        "universe": [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "ITC", "SBIN", "KOTAKBANK",
        ],
    },
    "taiwan": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # CURRENCY CONVERSION GAP: no direct USD/TWD pair exists on IB at all
        # (tested on both the plain and newer ibapi_forex clients -- genuine
        # 'No security definition' on both, not a version issue; likely a real
        # currency-control restriction on offshore retail FX for this
        # currency). Plain international-equities scanner only until/unless IB
        # ever offers this pair.
        "exchange": "TWSE",
        "currency": "TWD",
        "timezone": "Asia/Taipei",
        "open_time": "09:00",
        "close_time": "13:33",
        "sessions": [("09:00", "13:33")],
        "universe": [
            "2330", "2317", "2454", "2308", "1301", "2412", "2882", "1216",
        ],
    },
    "taiwan_otc": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # CURRENCY CONVERSION GAP: no direct USD/TWD pair exists on IB at all
        # (tested on both the plain and newer ibapi_forex clients -- genuine
        # 'No security definition' on both, not a version issue; likely a real
        # currency-control restriction on offshore retail FX for this
        # currency). Plain international-equities scanner only until/unless IB
        # ever offers this pair.
        "exchange": "TPEX",
        "currency": "TWD",
        "timezone": "Asia/Taipei",
        "open_time": "09:00",
        "close_time": "13:30",
        "sessions": [("09:00", "13:30")],
        "universe": [
            "6488", "5347", "3105", "6547", "3529",
        ],
    },
    "malaysia": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # CURRENCY CONVERSION GAP: no direct USD/MYR pair exists on IB at all
        # (tested on both the plain and newer ibapi_forex clients -- genuine
        # 'No security definition' on both, not a version issue; likely a real
        # currency-control restriction on offshore retail FX for this
        # currency). Plain international-equities scanner only until/unless IB
        # ever offers this pair.
        "exchange": "BURSAMY",
        "currency": "MYR",
        "timezone": "Asia/Kuala_Lumpur",
        "open_time": "09:00",
        "close_time": "17:00",
        "sessions": [("09:00", "12:30"), ("14:30", "17:00")],
        "universe": [
            "MAYBANK", "PBBANK", "TENAGA", "CIMB", "PCHEM", "IHH", "AXIATA",
        ],
    },
    "shanghai_connect": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/CNH pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "SEHKNTL",
        "currency": "CNH",
        "timezone": "Asia/Shanghai",
        "open_time": "09:30",
        "close_time": "15:00",
        "sessions": [("09:30", "11:30"), ("13:00", "15:00")],
        "universe": [
            "600519", "601318", "600036", "601398", "600276",
        ],
    },
    "sse_star": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/CNH pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "SEHKSTAR",
        "currency": "CNH",
        "timezone": "Asia/Shanghai",
        "open_time": "09:30",
        "close_time": "15:00",
        "sessions": [("09:30", "11:30"), ("13:00", "15:00")],
        "universe": [
            "688981", "688111", "688036", "688012",
        ],
    },
    "shenzhen": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/CNH pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "SEHKSZSE",
        "currency": "CNH",
        "timezone": "Asia/Shanghai",
        "open_time": "09:30",
        "close_time": "15:00",
        "sessions": [("09:30", "11:30"), ("13:00", "15:00")],
        "universe": [
            "000001", "000002", "000858", "002415",
        ],
    },
    "canada_venture": {
        # 2026-09-04: added as part of a comprehensive sweep of every
        # market IB's own reqScannerParameters() lists (the authoritative
        # candidate list, not a guess) -- real live verification via
        # reqContractDetails + reqMktData w/ reqMarketDataType(3) delayed
        # fallback + reqHistoricalData, same discipline as every other
        # entry in this dict. Every universe symbol below individually
        # confirmed to resolve via live reqContractDetails.
        # Currency conversion: real USD/CAD pair confirmed live, but needs the
        # newer ibapi_forex client (protocol 163+, error 10285 on the plain
        # client) -- same situation as EUR/USD/GBP/USD, added to
        # NEW_CLIENT_FOREX_PAIRS.
        "exchange": "VENTURE",
        "currency": "CAD",
        "timezone": "America/Toronto",
        "open_time": "09:30",
        "close_time": "16:00",
        "sessions": [("09:30", "16:00")],
        "universe": [
            "AAB",
        ],
    },
}

# How many of each market's universe, ranked by 5-day % change, get
# passed through to regime classification each cycle.
INTERNATIONAL_TOP_N_MOVERS = 5

# Per matched_strategy, symbols with real evidence that strategy doesn't
# work on them — same exclusion philosophy as mean_reversion's DIA
# exclusion (project_mean_reversion_future_forex memory), NOT a guess.
# Deliberately empty: no backtest/live history exists yet for these
# markets under these strategies (built 2026-09-02) — populate this once
# real evidence comes in, don't pre-guess which symbols will be bad fits.
INTERNATIONAL_STRATEGY_EXCLUSIONS = {
    "mean_reversion": [],
    "vwap": [],
    "reversal": [],
}

# Noise filter applied to raw scanner hits before they ever reach regime
# classification — drops illiquid/penny-stock junk that a raw "top
# gainers" list is otherwise full of.
INTERNATIONAL_MIN_PRICE = 2.0
INTERNATIONAL_MIN_AVG_VOLUME = 100_000

# Forex. USD/JPY trades on the main IB side-channel connection (self.app,
# clientId=2) unchanged. EUR/USD and GBP/USD were blocked there by an
# ibapi client-library version limit — PyPI's ibapi (what Lumibot's own
# broker depends on, pinned exactly) implements protocol only up to
# version 157, but these two pairs' "fractional size rules" need 163+.
# Fixed 2026-09-02 WITHOUT touching Lumibot's own dependency (real risk
# to the entire live connection, not just forex): vendored IBKR's own
# newer official client under ibapi_forex/ (renamed top-level package,
# every internal import rewritten from `ibapi.` to `ibapi_forex.` —
# downloaded directly from interactivebrokers.github.io, not PyPI, whose
# ibapi listing hasn't been updated at all). Confirmed live: the old
# client can't even fetch historical bars for these two pairs (error
# 10285), the new one fetches real data immediately. These two pairs use
# a SEPARATE connection (self.forex_app, clientId=3) — see NEW_CLIENT_
# FOREX_PAIRS in ib_side_channel_trader.py — so USD/JPY and everything
# else stays exactly as proven, untouched.
FOREX_PAIRS = [
    {"symbol": "USD", "currency": "JPY", "exchange": "IDEALPRO"},
    {"symbol": "EUR", "currency": "USD", "exchange": "IDEALPRO"},
    {"symbol": "GBP", "currency": "USD", "exchange": "IDEALPRO"},
    # 2026-09-04: added alongside the comprehensive INTERNATIONAL_MARKETS
    # sweep above, closing the currency-conversion gap for every new
    # market whose currency has a real, live-verified direct USD pair on
    # IB. All quoted as "USD"/symbol + native currency, same convention
    # as USD/JPY above (not the EUR/GBP "base"/USD convention) — verified
    # against real IB quotes, not assumed from the other pairs' shape.
    # These 8 work on the SAME plain connection as USD/JPY (self.app) —
    # confirmed live, no NEW_CLIENT_FOREX_PAIRS entry needed:
    {"symbol": "USD", "currency": "MXN", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "DKK", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "RON", "exchange": "IDEALPRO"},
    # USD/RUB deliberately excluded (2026-09-04) — see the "russia"
    # exclusion note above INTERNATIONAL_MARKETS: real sanctions
    # restrictions, not a technical limitation.
    {"symbol": "USD", "currency": "ILS", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "SAR", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "AED", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "KRW", "exchange": "IDEALPRO"},
    # These 5 need the newer ibapi_forex client (protocol 163+, error
    # 10285 on the plain connection — same situation as EUR/USD/GBP/USD)
    # — added to NEW_CLIENT_FOREX_PAIRS in both ib_connector.py and
    # ib_side_channel_trader.py:
    {"symbol": "USD", "currency": "CHF", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "CZK", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "NOK", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "CNH", "exchange": "IDEALPRO"},
    {"symbol": "USD", "currency": "CAD", "exchange": "IDEALPRO"},
    # BRL/INR/TWD/MYR deliberately NOT here — tested live on BOTH the
    # plain and newer clients, genuine "No security definition" on
    # both (not a version issue), most likely a real currency-control
    # restriction on offshore retail FX for these four. brazil/india/
    # taiwan/taiwan_otc/malaysia above stay scanner-only (no pod-style
    # currency-converted ordering) until/unless IB ever offers a direct
    # pair for one of these.
]

# "Tradeable universe" — the currencies this account can ACTUALLY trade in
# right now (2026-09-09), not just markets IB has data/a forex pair for.
# Found live: IB enforces a hard $2,000-USD-equivalent MINIMUM ACCOUNT
# BALANCE before it will process ANY order in a currency this account
# hasn't already established real exposure in — confirmed via a real
# order test (India/INR, rejected: "MINIMUM OF 2000 USD... REQUIRED...
# TO TRADE CURRENCY") and reconfirmed with a second, much cheaper test
# (KOTAKBANK/LICI, ~$4.60-4.70/share, same exact rejection — so this is
# NOT proportional to trade size). Crucially, this ALSO blocked Germany
# (RWE, EUR) even though EUR/USD is a real, live IB forex pair — the
# gate is about whether THIS ACCOUNT has real trading history in that
# currency, not just whether IB offers a conversion path for it. See
# project_ib_currency_and_scanner_findings_2026_09_09 memory for the
# full investigation.
#
# Established via this account's own real per-currency ledger (a real
# non-zero historical balance, from a real past trade) — GBP is the
# account's own base currency, always reachable. Re-verify this list
# periodically (a live whatIf order test, same as the investigation
# above) rather than assuming it's permanent — it will grow as real
# capital crosses the $2,000 floor and/or as more currencies get traded.
ACCOUNT_ESTABLISHED_CURRENCIES = {
    "GBP",  # base currency
    "USD",  # confirmed: real non-zero historical balance (-$41.36 at last check)
    "JPY",  # confirmed: real non-zero historical balance (-¥1740 at last check)
    "MXN",  # confirmed: real non-zero historical balance (-$1.44 at last check)
    # CZK deliberately NOT included despite a real past trade (MONET) --
    # that position closed and the ledger showed 0.00 CZK at last check,
    # so whether CZK is still "established" post-closure is UNVERIFIED,
    # not assumed either way. Test live before trusting it.
}

# Same IB Gateway the main bot connects to (INTERACTIVE_BROKERS_IP/PORT
# in .env) — ib_side_channel_trader.py just uses a different CLIENT_ID
# on the same gateway (one IB Gateway process supports multiple
# simultaneous API client connections). 2026-09-02: this single
# connection now also carries US extended-hours (pre/post-market)
# trading, previously a separate Alpaca connection — user call, "make
# it one channel not many" — see project_stop_and_restart_loop_fixes
# memory for the mismatch this fixed.
INTERNATIONAL_MARKETS_IP = os.environ.get("INTERACTIVE_BROKERS_IP", "127.0.0.1")
INTERNATIONAL_MARKETS_PORT = os.environ.get("INTERACTIVE_BROKERS_PORT", "4002")

# Market-close-aware trading gate (2026-09-02, user call): for any
# session where a position becomes genuinely unreachable once it ends
# (US extended-hours' post-market cutoff, each international market's
# own close — NOT the main regular-hours bot, which just continues
# managing overnight/multi-day holds next session as designed, and NOT
# forex, which trades near-continuously) — stop opening new positions,
# and force-liquidate anything still open, inside this buffer before
# that session ends. There's no reliable way to forecast whether a
# fresh trade would be profitable by close, so the buffer blocks new
# entries outright rather than guessing.
MARKET_CLOSE_BUFFER_MINUTES = 30

# ============================================================================
# TRADING HOURS
# ============================================================================

# Regular-hours reference point (ET) — used by ib_side_channel_trader.py
# to compute the real pre-market start (open - 5.5h = 4:00am) and
# post-market end (close + 4h = 8:00pm), the full window IB actually
# provides US equity quotes for. Not a restriction on when we trade —
# actual extended-hours trading is controlled by the dashboard's own
# toggle (/api/extended-hours-trading), not a flag here. (2026-09-03:
# removed ENABLE_PREMARKET/ENABLE_AFTERHOURS, dead flags from before the
# side-channel unification — never referenced anywhere, and misleadingly
# read as "disabled" while controlling nothing.)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

# ============================================================================
# API & WEB SERVER
# ============================================================================

FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))
FLASK_DEBUG = os.environ.get("FLASK_ENV", "production") == "development"

# Shared secret required (as X-API-Key header, ?key= query param, or the
# socket.io connection query) to reach any /api/* route or receive
# WebSocket broadcasts — added 2026-08-31 after the API was found publicly
# reachable (FLASK_HOST=0.0.0.0) with no authentication at all. Empty
# means auth is disabled (e.g. local dev) — the dashboard has been live
# and configured with a real key since 2026-08-31, so this should not be
# empty in that deployment.
DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "")

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
