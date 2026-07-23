# Short the Dump

Short the Dump is an open-source, Python research platform for exchange-listed, low-float small-cap momentum events. It detects extreme Day 0 price/volume moves, builds a point-in-time evidence file, rates observable momentum failure, applies broker-specific short-execution and account-risk gates, and compares same-day, Day +1, first-red-day, and Day +2 short simulations.

The safe default is **alert-only**. This release requires a human decision for every candidate, cannot average into a losing short, and will not enable live routing without explicit configuration and a deployment-time acknowledgement. The included symbols, locates, filings, prices, and results are synthetic fixtures.

> This is research software, not investment advice. Low-float shorts can gap far beyond a stop, remain halted, become impossible to borrow, be recalled, or produce losses larger than planned or deposited capital. A displayed score is an explainable rule rating, not a calibrated probability of profit.

## What is implemented

- Append-oriented SQLite event store with `effective_at` and `observed_at` timestamps, point-in-time reads, source references, content hashes, and a SHA-256 chained audit log.
- Configurable exchange, price, float, market-cap, Day 0 return, relative-volume, and dollar-volume detector.
- Evidence extraction for float turnover, VWAP distance, close off high, lower highs, failed VWAP reclaims, opening-range breakdown, volume fade, trading halts, and social-attention z-scores.
- Transparent catalyst classification across novelty, source credibility, materiality, and promotional risk.
- SEC/share-supply analysis for shelf and resale registrations, ATM language, equity lines, warrants, convertibles, reverse splits, registered capacity, and authorized-share overhang.
- Broker-specific execution snapshots covering shortability, available shares, locate requirement/cost/expiry, borrow fee, spread, margin, buying power, hard-to-borrow state, and Rule 201 price-test support.
- Independent execution rejection and position-risk engines. Stale inventory, impossible size, expensive borrow, wide spread, insufficient liquidity, active halt, expired locate, unsupported Rule 201 handling, margin shortfall, daily loss, and gross exposure can all block a trade.
- Manual alert review, internal paper-order router, tamper-evident audit trail, and a hard no-pyramiding/no-losing-short-add path.
- Event-driven simulator with volume participation, partial fills, spread and impact slippage, halts, changing borrow fees, locate cost, recalls, maximum holding periods, forced-buy-in stress, and chronological walk-forward testing.
- Local FastAPI dashboard for candidates, evidence, source freshness, broker checks, historical analogs, simulated performance, risk limits, and audit activity.
- Normalized connector protocols, deterministic JSONL replay, a broker inventory replay adapter, and a read-only SEC submissions adapter.
- Continuous polling orchestration that joins market, halt, news, social, filing, broker-inventory, and account-risk adapters at one UTC cutoff before producing a candidate.

## What is deliberately not claimed

No bundled connector supplies live market, social, news, or broker inventory data without the operator's vendor credentials and entitlements. The SEC adapter obtains filing metadata; production filing-text extraction and XBRL normalization remain connector responsibilities. No production live-order router ships in this release. Those are explicit deployment integrations, not silently substituted demo data.

Backtest outputs are only as trustworthy as the captured point-in-time data. You need historical NBBO/quotes, halts, broker inventory, locate quotes, borrow changes, recalls, corporate actions, and delisted symbols to make execution claims. See [Live readiness](docs/live-readiness.md).

## Quick start

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\short-the-dump --config config/default.toml seed-demo
.\.venv\Scripts\short-the-dump --config config/default.toml serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The demo banner identifies all fixture-backed content. On macOS/Linux, use `.venv/bin/short-the-dump`.

To keep test data separate:

```powershell
.\.venv\Scripts\short-the-dump --db demo.db seed-demo
.\.venv\Scripts\short-the-dump --db demo.db serve
```

### Run research checks

```powershell
.\.venv\Scripts\short-the-dump --db demo.db candidates
.\.venv\Scripts\short-the-dump --db demo.db backtest-demo
.\.venv\Scripts\short-the-dump --db demo.db backtest-demo --stress
.\.venv\Scripts\short-the-dump --db demo.db walk-forward-demo
.\.venv\Scripts\short-the-dump --db demo.db verify-audit
```

### Enable dashboard review actions

Mutation endpoints are disabled unless a local operator token exists. This does not enable order routing.

```powershell
$env:SHORT_THE_DUMP_OPERATOR_TOKEN = "use-a-long-random-local-token"
.\.venv\Scripts\short-the-dump --db demo.db serve
```

In `alert_only`, approval records a reviewed research file and creates no order. In `paper`, it creates an internal paper order after the execution and risk gates pass. The CLI approval path also requires an operator and reason:

```powershell
.\.venv\Scripts\short-the-dump --config config/default.toml approve DECISION_ID `
  --operator "Jane Analyst" --reason "Reviewed catalyst, filing, halt, locate, and stop evidence"
```

## Architecture

```text
market/news/social/SEC adapters       broker inventory adapter
              │                               │
              ▼                               ▼
      append-only observations       broker-specific snapshot
              │                               │
              ├──── point-in-time feature file ────┐
              │                                     │
              ▼                                     ▼
    explainable reversal rating             execution gate
              │                                     │
              └──────────────┬──────────────────────┘
                             ▼
                    independent risk sizing
                             │
                    alert + manual decision
                             │
                  alert-only / paper by default
```

The feature engine can only read facts whose `observed_at` and `effective_at` are at or before its evaluation cutoff. Broker feasibility never changes the research score; it changes whether the proposed trade can exist. Risk sizing never changes the score either; it can only reduce size or reject the plan.

See [Architecture](docs/architecture.md) and [Data contract](docs/data-contract.md) for the detailed boundaries.

## Configuration

[`config/default.toml`](config/default.toml) is the documented baseline. [`config/conservative.toml`](config/conservative.toml) tightens detection, execution, and risk limits. Unknown sections or fields fail fast.

Key safety behavior:

- `runtime.mode = "alert_only"` by default.
- `manual_approval_required` must remain `true` in this release.
- `risk.no_averaging_into_losers` must remain `true`.
- `live_order_routing_enabled` is invalid outside `limited_live`.
- `limited_live` additionally requires `SHORT_THE_DUMP_LIVE_ACK=I_UNDERSTAND_LIVE_SHORT_RISK` and an explicitly supplied live router. No live router is bundled.

## Ingest normalized observations

Adapters normalize vendor payloads into JSON Lines. Each record separates when something happened (`effective_at`) from when the system knew it (`observed_at`).

```json
{"symbol":"ABCD","kind":"market_snapshot","effective_at":"2026-07-21T15:00:00Z","observed_at":"2026-07-21T15:00:02Z","source":"licensed-feed","source_ref":"feed://snapshot/123","payload":{"exchange":"NASDAQ","price":4.2,"previous_close":2.1,"cumulative_volume":12000000,"average_daily_volume":700000,"float_shares":3500000,"market_cap":42000000,"average_bar_dollar_volume":300000}}
```

```powershell
.\.venv\Scripts\short-the-dump --db research.db ingest-jsonl observations.jsonl --detect
```

The example is a schema illustration, not a live signal. Full field definitions and supported `kind` values are in [Data contract](docs/data-contract.md).

### Wire live adapters

Implement the protocols in `short_the_dump.connectors.base`, then pass them to `RealtimeMonitor`. Transports, credentials, exchange entitlements, and vendor rate limits remain inside the adapters.

```python
from short_the_dump.config import load_config
from short_the_dump.monitor import ConnectorBundle, RealtimeMonitor
from short_the_dump.service import ResearchService
from short_the_dump.store import EventStore

config = load_config("config/default.toml")
store = EventStore(config.runtime.database_path)
monitor = RealtimeMonitor(
    ResearchService(config, store),
    ConnectorBundle(
        market=my_market_adapter,
        news=my_news_adapter,
        social=my_social_adapter,
        filings=my_filing_adapter,
        broker=my_broker_inventory_adapter,
        account=my_account_state_adapter,
    ),
)

# Each tuple is (exchange symbol, SEC CIK). Adapter errors are isolated per symbol.
monitor.run_forever([("ABCD", "0000123456")], interval_seconds=30)
```

The monitor first runs the extreme-move detector. It does not query expensive broker inventory or create a decision for non-candidates. A provider mismatch or polling failure is written to the audit chain.

## API

Run the server and open `/api/docs` for the generated schema. Core read endpoints:

- `GET /api/health`
- `GET /api/summary`
- `GET /api/candidates`
- `GET /api/candidates/{decision_id}`
- `GET /api/backtests`
- `GET /api/audit`

Review endpoints require `X-Operator-Token` and a body containing `operator` and `reason`.

## Backtest interpretation

The simulator intentionally applies pessimistic costs. It:

- caps entry and cover fills by a configured share of bar volume;
- crosses a fraction of the recorded spread and adds nonlinear participation impact;
- skips halted bars and applies an observable-uptick proxy when Rule 201 is active;
- caps entry size at point-in-time inventory and rejects missing/recalled borrow;
- charges locate cost and time-varying annualized borrow;
- forces a cover after a recall delay or stress-scenario buy-in;
- marks residual end-of-data shares at an adverse stressed price rather than dropping them.

Walk-forward selection ranks parameter sets only on each chronological training window, admits a plateau near the best objective, chooses a central member of that plateau, and reports its next untouched test window. Robust ranges show supported plateaus instead of a single best-looking parameter.

## Development

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
```

The core engine uses the standard library. FastAPI/Uvicorn serve the local dashboard; pytest/httpx/ruff are development dependencies.

## Project status

This is an alpha research foundation. Before any limited live pilot, complete every item in [Live readiness](docs/live-readiness.md), perform an independent code and model review, reconcile the selected broker's real short-sale semantics, and operate paper mode through representative halts, rejects, stale feeds, locate expirations, recalls, and disconnects.

Licensed under Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
