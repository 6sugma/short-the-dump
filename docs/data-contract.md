# Normalized data contract

## Observation envelope

Every external fact becomes an `Observation` with:

| Field | Meaning |
|---|---|
| `symbol` | Uppercase exchange symbol at the observation time |
| `kind` | Normalized payload family |
| `effective_at` | UTC timestamp when the source fact applies |
| `observed_at` | UTC timestamp when this system received/learned the fact |
| `source` | Vendor, regulator, broker, or fixture identity |
| `source_ref` | Stable vendor ID or safe source URL, when available |
| `event_id` | Optional associated Day 0 event |
| `payload` | Kind-specific normalized values |

Naive timestamps are interpreted as UTC by the domain layer, but production adapters should always submit timezone-aware UTC values. Never set `observed_at` equal to an earlier source timestamp when the data arrived later; doing so introduces look-ahead.

## Supported observation kinds

### `market_snapshot`

Required for detection and feature creation:

- `exchange`
- `price`
- `previous_close`
- `cumulative_volume`
- `average_daily_volume`
- `float_shares`

Strongly recommended:

- `session_high`, `vwap`, `dollar_volume`
- `shares_outstanding`, `market_cap`
- `average_bar_dollar_volume`

Float is a sourced point-in-time estimate. Retain its vendor/as-of lineage and do not backfill a later corrected float into old events.

### `bar`

- `open`, `high`, `low`, `close`, `volume`
- optional `vwap`, `bid`, `ask`, `halted`, `session_index`

The current feature engine assumes one-minute bars for opening-range minutes. A different bar interval requires matching configuration/adapter semantics.

### `news`

- `headline` or `title`
- optional `summary`, `body`, `issuer_press_release`, `paid_promotion`, `stated_value`

Keep earlier news in the store so novelty can be measured against what existed before Day 0.

### `social`

- either `zscore`; or
- `mentions`, `baseline_mean`, `baseline_std`

Store platform/query coverage in the payload or source metadata. Mention counts from different providers are not automatically comparable.

### `filing`

- `form`
- optional `title`, `text`, `status`, `accession`, `filing_date`, `report_date`

The SEC metadata adapter emits blank `text` because document retrieval, parsing, and section selection require deployment-specific caching and rate control. A production filing connector should populate text only after the document was actually available and record that retrieval as `observed_at`.

### `capital_structure`

- optional `authorized_shares`, `shares_outstanding`, `float_shares`
- optional `registered_offering_remaining`, `market_cap`
- optional `last_reverse_split_date`

### `halt`

- recommended `reason`, `resumed_at`

Halt observations and halted bars can coexist. The evidence file counts markers; production normalization should deduplicate a vendor's repeated halt messages with stable `source_ref` values.

## Broker snapshot

Broker data does not use the generic observation envelope because it has a dedicated immutable record and tighter freshness semantics. Required fields include:

- broker name and event ID;
- observed timestamp;
- shortable flag and available shares;
- locate requirement, per-share cost, and optional expiry;
- annualized borrow fee;
- bid and ask;
- margin requirement and buying power;
- Rule 201 state and whether the adapter can enforce the price test;
- hard-to-borrow state and broker notes.

Never copy inventory from one broker into another broker's snapshot. Never assume an easy-to-borrow flag means a quantity is available. Re-query immediately before any order.

## Replay contract

An `EventReplay` holds:

- the original Day 0 event;
- chronological bars across all candidate holding sessions;
- chronological broker borrow/inventory points;
- signal timestamp;
- desired quantity;
- Rule 201 state and spread fallback.

For credible research, preserve symbols that delisted, corporate actions, session calendars, zero-volume/halted intervals, quote spreads, locate failures, rejects, recalls, and forced buy-ins. A bars-only dataset cannot validate short executability.
