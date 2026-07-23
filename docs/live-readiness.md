# Live-readiness gate

This release is ready for local research, alert-only operation, and controlled internal paper simulation. It is not a production live-trading system. Do not add a live router until every control below has an owner, test evidence, and rollback procedure.

## Data and causality

- Licensed real-time and historical feeds have explicit exchange entitlements.
- Every adapter preserves receive time separately from source event time.
- Universe history retains delisted names and point-in-time exchange/listing status.
- Float, shares outstanding, splits, corporate actions, and offering capacity are point-in-time.
- Historical news/social/filings reflect original availability, revisions, and deletions.
- Halt and LULD state is reconciled against an authoritative source.
- Feed sequence gaps, clock drift, stale data, and reconnect backfills produce visible degraded state and block orders.

## Broker and Regulation SHO

- The selected broker adapter is reviewed against its current API and account agreement.
- Shortable shares, locate quotes, locate IDs, expiry, fees, margin, and buying power are re-queried at order time.
- Rule 201 state and price-test order handling have broker-confirmed semantics.
- Locate acceptance, order rejects, partial fills, cancellations, replacements, and duplicate client IDs are idempotent.
- Hard-to-borrow fee changes, recalls, buy-ins, and close-out requirements are captured and reconciled.
- Restricted securities, threshold-list conditions, corporate actions, and account-level restrictions block trading.
- The operator has obtained legal/compliance review applicable to jurisdiction, broker, entity, strategy, and automation level.

## Risk and operations

- Account equity, realized/unrealized P&L, open orders, positions, and buying power are reconciled with the broker before sizing.
- Daily loss, gross exposure, symbol loss, concurrency, and no-loser-add controls have integration tests against real broker states.
- A broker-side or independent catastrophic stop policy addresses halts and process failure; local stops alone are insufficient.
- Kill switches work during partial outages and have a human runbook.
- Credentials are stored in a secrets manager, least-privileged, rotated, and absent from logs/database/dashboard.
- Operator authentication, authorization, session expiry, CSRF protection, and TLS are added before network exposure.
- Monitoring covers feed age, broker age, order state mismatch, audit failure, process health, disk space, and time synchronization.
- Backups and restore drills include the event store and audit evidence.

## Research validation

- Walk-forward windows cover multiple regimes and a representative number of independent events.
- Results survive wider spreads, worse slippage, lower participation, locate failures, borrow spikes, recalls, buy-ins, halts, and gap stops.
- Same-day, Day +1, first-red-day, and Day +2 variants use identical eligible universes and cost assumptions.
- Parameter conclusions are plateaus with stable OOS behavior, not a single optimized point.
- Multiple-testing and strategy-selection bias are disclosed.
- An independent reviewer reproduces the event set, features, fills, costs, and summary metrics from raw snapshots.
- Paper mode runs long enough to observe rejects, stale locates, partial fills, halts, disconnects, and end-of-day reconciliation.

## Limited pilot

If all gates pass, begin with the smallest separately funded account and stricter limits than the research configuration. Retain manual approval, one open position, no averaging, small notional, daily shutdown, and immediate operator visibility. Expand only from reconciled real fills and incident-free operation—not backtest performance.
