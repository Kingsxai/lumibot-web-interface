# Lumibot Multi-Strategy Trading Bot with Web Interface

A paper-trading bot (Alpaca) running many independent Lumibot strategies at
once, with a Flask/SocketIO dashboard for monitoring and manual control. A
brief Interactive Brokers detour (2026-09-02 through 2026-09-10) was fully
reverted — the account is Alpaca-only today. See `CLAUDE.md` for the full,
current architecture; this file only covers getting it running.

## What's actually running

- **9 core strategies** (momentum, breakout, mean reversion, VWAP, gap-and-go,
  reversal, market profile, scalping, scalping v2) pulling candidates from a
  shared, regime-routed symbol pool (`regime_router.py`) rather than each
  strategy keeping its own list.
- **14 additional strategies** (`extended_strategies_live.py`) from a later
  sizing/collision-testing round — real code, seeded OFF by default; check
  the dashboard's strategy toggles before assuming any of them is trading.
- Secondary-indicator confirmation, a VIX "second opinion" rider, a Phase 4
  meta-model confidence gate, an exclusive per-symbol ownership lock, a
  fixed-priority takeover system, and a news-sentiment emergency stop that
  can liquidate any position regardless of which strategy opened it.
- News sentiment is a symbol-selection input for scalping, not its own
  strategy — it never places orders on its own.
- A Phase 5 human-vs-AI paper-trading proposal pipeline (`proposals/`) — see
  `CLAUDE.md` for details; unrelated to whether the main bot above is trading.

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a `.env` file in the project root

```bash
BROKER_PROVIDER=alpaca
ALPACA_API_KEY=your-alpaca-key
ALPACA_API_SECRET=your-alpaca-secret
ALPACA_IS_PAPER=true

# Required on every /api/* request (header X-API-Key, or ?key=) and the
# socket.io handshake -- the dashboard has zero auth without this set.
DASHBOARD_API_KEY=pick-a-random-string

FLASK_HOST=127.0.0.1
FLASK_PORT=5000
FLASK_ENV=production
```

The bot loads this via `python-dotenv` (`load_dotenv()` in `bot_runner.py`) —
plain shell `export`s work too, but `.env` is what's actually used day to
day. `BROKER_PROVIDER=interactive_brokers` still exists as a code path but is
untested/dormant since the reversal; don't rely on it without re-verifying
the IB-specific pieces first (see `CLAUDE.md`).

### 3. Run the bot

```bash
python bot_runner.py
```

Starting the process does **not** start trading — that's a deliberate
manual gate. Open the dashboard (`http://<FLASK_HOST>:<FLASK_PORT>`, default
`http://127.0.0.1:5000`) and click **Start**.

## Architecture

```
 Dashboard (templates/dashboard.html)
        |  HTTP + WebSocket, X-API-Key required
        v
 Flask/SocketIO API (api.py) -- ~30 routes
        |
        v
 MultiStrategyBot (strategy_manager.py) -- Lumibot Strategy subclass
        |  candidates from regime_router.py (shared pool, all strategies)
        |  confirmation.py (secondary check + VIX rider)
        |  meta_model.py (Phase 4 confidence gate)
        v
 Lumibot -> Alpaca broker (paper account, real Alpaca infrastructure)
```

## Configuration

Edit `config.py` for: position sizing (`MAX_POSITION_SIZE`, a percentage,
and `HARD_POSITION_CEILING_GBP`, a flat dollar ceiling that actually binds
at this account's size), stop-loss/take-profit defaults, `STRATEGY_PRIORITY_RANK`
(who wins a same-symbol conflict), and the asset-class rider toggles
(`ENABLE_VIX_RIDER` etc.). Most of these are also editable live from the
dashboard without a restart.

## API

`api.py` exposes ~30 routes under `/api/*`, all requiring the
`DASHBOARD_API_KEY` above. Key ones:

- `GET /api/status`, `GET /api/system-health` — bot state, heartbeat
- `GET /api/positions`, `GET /api/orders`, `GET /api/bracket-orders`, `GET /api/recent-closed-orders`
- `POST /api/orders` — place a manual order
- `POST /api/positions/<symbol>/liquidate` — close one position
- `POST /api/start`, `POST /api/stop` (liquidates everything — see `CLAUDE.md`), `POST /api/pause-new-entries` (pause only, keeps existing positions' exits active)
- `GET /api/strategy-toggles`, `POST /api/strategy-toggles` — enable/disable a strategy
- `GET /api/strategy-risk`, `POST /api/strategy-risk` — per-strategy stop-loss/take-profit override
- `GET /api/strategy-preview` — dry-run every strategy's current signal without placing orders
- `GET /api/confidence-stats`, `GET /api/regime-stats`, `GET /api/performance-overview`
- `GET /api/account-mode`, `POST /api/account-mode`, `POST /api/live-credentials` — paper/live switch + credential storage
- `POST /api/test-all-strategies` — sweep test, `GET /api/test-all-strategies/status` for progress

Check `api.py` directly for exact request/response shapes before building
against any of these — they aren't all documented here in full.

## Safety

- **Paper trading account, real Alpaca infrastructure** — this isn't a
  simulator; it connects to Alpaca's actual paper endpoint.
- Trading requires an explicit dashboard **Start** click every time the
  process (re)starts — never auto-resumes on its own except a narrow,
  watchdog-only safety-resume path that only re-enables existing positions'
  stop-loss/take-profit protection, never new entries.
- `POST /api/stop` liquidates **every** open position, winners and losers
  alike. Use `POST /api/pause-new-entries` if you just want to stop opening
  new positions while letting existing ones run their exits.
- `.env` holds live API credentials — never commit it.
