# Architecture

## Safety and causality boundaries

Short the Dump treats a research fact, an execution fact, and a risk decision as different objects.

1. An observation records `effective_at` (when the underlying fact applies) and `observed_at` (when this system first knew it). Feature queries require both timestamps to be no later than the evaluation cutoff.
2. A feature snapshot is immutable and stores its `as_of` cutoff. The reversal score is computed only from that snapshot.
3. A broker snapshot is immutable and broker-specific. It is never inferred from price behavior or borrowed from another broker.
4. The execution gate either supplies a realistically capped share quantity or rejects the proposal.
5. The risk engine can reduce that quantity or reject it. It cannot enlarge the execution quantity.
6. An alert is not an order. A named operator and reason are required. Alert-only mode cannot create an order.
7. Every state change and captured evidence item is added to a hash-chained audit log.

## Package map

| Module | Responsibility |
|---|---|
| `models.py` | Immutable domain records, enums, JSON conversion, identifiers |
| `config.py` | TOML settings and non-negotiable safety validation |
| `store.py` | SQLite schema, point-in-time queries, decisions, orders, audit chain |
| `features.py` | Detection, catalyst/supply analysis, market-failure features, analog similarity |
| `execution.py` | Broker-specific feasibility and realistic size cap |
| `risk.py` | Kill switch, stop/gap-aware sizing, exposure limits, no-loser-add rule |
| `scoring.py` | Explainable rating band and candidate evidence file |
| `workflow.py` | Manual approval, alert-only behavior, internal paper router |
| `backtest.py` | Event-driven fills, borrow, halts, recalls, stress, walk-forward selection |
| `service.py` | Orchestration across storage, features, execution, risk, and decisions |
| `connectors/` | Vendor/broker protocols, replay adapters, read-only SEC adapter |
| `monitor.py` | Continuous multi-source polling at a single point-in-time cutoff |
| `api.py` | Local read APIs and token-gated review endpoints |
| `web/` | Dependency-free operational dashboard assets |

## Point-in-time storage

`observations` is append-oriented. A corrected vendor fact is a new row with a later `observed_at`; it does not overwrite history. The feature engine performs a visibility check even after the database query, providing a second guard against future facts.

Event status, decision review status, and orders are mutable operational state. Every transition is committed in the same SQLite transaction as an audit record. The audit hash covers the prior hash, timestamp, actor, action, entity identity, and canonical details JSON.

SQLite runs with foreign keys, a busy timeout, and WAL mode for file-backed databases. It is appropriate for one local research process and modest concurrent readers. A multi-host deployment should implement the same store contract on a transactional database and retain append/audit semantics.

## Evidence rating

The rating combines four explainable families:

- Observable momentum failure: below VWAP, close off high, lower highs, failed reclaim bars, opening-range breakdown, and volume fade.
- Catalyst fragility: low materiality, repeated language, and promotional risk.
- Share-supply risk: registration/offering forms and language, warrants/convertibles, capacity, and reverse-split history.
- Exhaustion context: relative volume, reported float turnover, and close off high.

Halt count reduces actionability because it increases gap and fill risk. A novel material catalyst is also surfaced as a risk flag. The weights are configuration/code assumptions—not learned probabilities—and must be validated out of sample.

## Execution and order boundaries

The execution gate records a result for every check, including measured values and limits. It caps shares by:

- broker-reported inventory;
- configured participation in average bar dollar volume;
- buying power after broker margin requirement; and
- requested quantity.

The risk engine then caps this quantity by stop plus halt-gap reserve, per-trade loss budget, maximum position notional, and remaining gross-short capacity.

Only `ApprovalWorkflow` can create an order. It checks for an existing order on the event to prevent pyramiding. The bundled router is internal paper only. Supplying a live router is an application-level integration and still requires the configuration and environment interlocks.

## Backtest boundary

The simulator takes already reconstructed `EventReplay` objects. A replay should contain every point-in-time bar and borrow state available to the strategy. Universe construction, delisting retention, corporate-action adjustment, and vendor normalization happen upstream so they can be audited independently.

The walk-forward optimizer never reports training performance as OOS. A plateau rule reduces reliance on an isolated optimum, but it does not cure biased data, multiple testing, or a small sample.
