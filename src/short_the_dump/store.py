from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import (
    Approval,
    BacktestResult,
    BrokerSnapshot,
    CandidateDecision,
    EventCase,
    EventStatus,
    Observation,
    canonical_json,
    ensure_utc,
    jsonable,
    parse_datetime,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    day0_date TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    baseline_close REAL NOT NULL,
    detection_price REAL NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_symbol_day ON events(symbol, day0_date);
CREATE INDEX IF NOT EXISTS idx_events_status_detected ON events(status, detected_at DESC);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(id),
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_pit
    ON observations(symbol, kind, observed_at, effective_at);
CREATE INDEX IF NOT EXISTS idx_observations_event
    ON observations(event_id, kind, observed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_idempotency
    ON observations(symbol, kind, source, source_ref, observed_at)
    WHERE source_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    as_of TEXT NOT NULL,
    score REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_features_event_asof ON feature_snapshots(event_id, as_of DESC);

CREATE TABLE IF NOT EXISTS broker_snapshots (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    broker TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_event_time
    ON broker_snapshots(event_id, broker, observed_at DESC);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    feature_snapshot_id TEXT NOT NULL REFERENCES feature_snapshots(id),
    created_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    score REAL NOT NULL,
    band TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_status_time
    ON candidate_decisions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_event_time
    ON candidate_decisions(event_id, created_at DESC);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL REFERENCES candidate_decisions(id),
    operator TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES candidate_decisions(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_event ON orders(event_id, created_at DESC);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(id),
    occurred_at TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    stress_label TEXT NOT NULL,
    total_pnl REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backtests_created ON backtest_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL UNIQUE
);
"""


class EventStore:
    """SQLite append-oriented store with point-in-time reads and a hash-chained audit log."""

    def __init__(self, path: str | Path = "short_the_dump.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(SCHEMA)
            self._connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '1')"
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        details: Mapping[str, Any] | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        conn = connection or self._connection
        timestamp = ensure_utc(occurred_at or utc_now()).isoformat()
        previous = conn.execute(
            "SELECT record_hash FROM audit_log ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["record_hash"] if previous else "GENESIS"
        details_json = canonical_json(details or {})
        material = "|".join(
            [previous_hash, timestamp, actor, action, entity_type, entity_id, details_json]
        )
        record_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO audit_log(
                occurred_at, actor, action, entity_type, entity_id,
                details_json, previous_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp,
                actor,
                action,
                entity_type,
                entity_id,
                details_json,
                previous_hash,
                record_hash,
            ),
        )
        if connection is None:
            conn.commit()
        return record_hash

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        rows = self._connection.execute("SELECT * FROM audit_log ORDER BY sequence").fetchall()
        previous_hash = "GENESIS"
        for row in rows:
            material = "|".join(
                [
                    previous_hash,
                    row["occurred_at"],
                    row["actor"],
                    row["action"],
                    row["entity_type"],
                    row["entity_id"],
                    row["details_json"],
                ]
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["record_hash"] != expected:
                return False, int(row["sequence"])
            previous_hash = row["record_hash"]
        return True, None

    def create_event(self, event: EventCase, actor: str = "scanner") -> EventCase:
        now = utc_now().isoformat()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM events WHERE symbol = ? AND day0_date = ?",
                (event.symbol, event.day0_date.isoformat()),
            ).fetchone()
            if existing:
                return self._event_from_row(existing)
            conn.execute(
                """INSERT INTO events(
                    id, symbol, exchange, day0_date, detected_at, status,
                    baseline_close, detection_price, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.symbol,
                    event.exchange,
                    event.day0_date.isoformat(),
                    ensure_utc(event.detected_at).isoformat(),
                    event.status.value,
                    event.baseline_close,
                    event.detection_price,
                    event.source,
                    now,
                ),
            )
            self.audit(
                actor,
                "event.created",
                "event",
                event.id,
                jsonable(event),
                connection=conn,
            )
        return event

    def get_event(self, event_id: str) -> EventCase | None:
        row = self._connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event_from_row(row) if row else None

    def find_event(self, symbol: str, day0_date: date) -> EventCase | None:
        row = self._connection.execute(
            "SELECT * FROM events WHERE symbol = ? AND day0_date = ?",
            (symbol.upper(), day0_date.isoformat()),
        ).fetchone()
        return self._event_from_row(row) if row else None

    def list_events(self, status: EventStatus | None = None, limit: int = 100) -> list[EventCase]:
        if status is None:
            rows = self._connection.execute(
                "SELECT * FROM events ORDER BY detected_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE status = ? ORDER BY detected_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def update_event_status(
        self, event_id: str, status: EventStatus, actor: str, reason: str
    ) -> None:
        with self.transaction() as conn:
            current = conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()
            if not current:
                raise KeyError(f"event not found: {event_id}")
            conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, utc_now().isoformat(), event_id),
            )
            self.audit(
                actor,
                "event.status_changed",
                "event",
                event_id,
                {"from": current["status"], "to": status.value, "reason": reason},
                connection=conn,
            )

    def append_observation(self, observation: Observation, actor: str = "ingest") -> bool:
        payload_json = canonical_json(observation.payload)
        content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO observations(
                        id, event_id, symbol, kind, effective_at, observed_at,
                        source, source_ref, payload_json, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation.id,
                        observation.event_id,
                        observation.symbol,
                        observation.kind,
                        ensure_utc(observation.effective_at).isoformat(),
                        ensure_utc(observation.observed_at).isoformat(),
                        observation.source,
                        observation.source_ref,
                        payload_json,
                        content_hash,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "idx_observations_idempotency" in str(exc) or "UNIQUE constraint failed" in str(
                    exc
                ):
                    return False
                raise
            self.audit(
                actor,
                "observation.appended",
                "observation",
                observation.id,
                {
                    "event_id": observation.event_id,
                    "symbol": observation.symbol,
                    "kind": observation.kind,
                    "source": observation.source,
                    "content_hash": content_hash,
                },
                connection=conn,
                occurred_at=observation.observed_at,
            )
        return True

    def observations_as_of(
        self,
        symbol: str,
        as_of: datetime,
        *,
        kinds: Sequence[str] | None = None,
        event_id: str | None = None,
    ) -> list[Observation]:
        cutoff = ensure_utc(as_of).isoformat()
        clauses = ["symbol = ?", "observed_at <= ?", "effective_at <= ?"]
        values: list[Any] = [symbol.upper(), cutoff, cutoff]
        if event_id is not None:
            clauses.append("(event_id = ? OR event_id IS NULL)")
            values.append(event_id)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            values.extend(kinds)
        query = (
            "SELECT * FROM observations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY effective_at, observed_at, id"
        )
        rows = self._connection.execute(query, values).fetchall()
        return [self._observation_from_row(row) for row in rows]

    def latest_observation(
        self,
        symbol: str,
        kind: str,
        as_of: datetime,
        event_id: str | None = None,
    ) -> Observation | None:
        values: list[Any] = [symbol.upper(), kind, ensure_utc(as_of).isoformat()]
        event_clause = ""
        if event_id is not None:
            event_clause = " AND (event_id = ? OR event_id IS NULL)"
            values.append(event_id)
        row = self._connection.execute(
            f"""SELECT * FROM observations
                WHERE symbol = ? AND kind = ? AND observed_at <= ?
                  AND effective_at <= ? {event_clause}
                ORDER BY observed_at DESC, effective_at DESC, id DESC LIMIT 1""",
            values[:3] + [values[2]] + values[3:],
        ).fetchone()
        return self._observation_from_row(row) if row else None

    def save_feature_snapshot(self, snapshot: Any, actor: str = "feature_engine") -> None:
        payload = jsonable(snapshot)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO feature_snapshots(id, event_id, as_of, score, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    snapshot.id,
                    snapshot.event_id,
                    ensure_utc(snapshot.as_of).isoformat(),
                    snapshot.momentum_failure_score,
                    canonical_json(payload),
                ),
            )
            self.audit(
                actor,
                "feature_snapshot.created",
                "feature_snapshot",
                snapshot.id,
                {
                    "event_id": snapshot.event_id,
                    "as_of": payload["as_of"],
                    "score": snapshot.momentum_failure_score,
                },
                connection=conn,
            )

    def latest_feature_payload(self, event_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM feature_snapshots WHERE event_id = ? ORDER BY as_of DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def feature_payloads(self, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT f.payload_json, e.symbol, e.day0_date
               FROM feature_snapshots f JOIN events e ON e.id = f.event_id
               ORDER BY f.as_of DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["symbol"] = row["symbol"]
            payload["day0_date"] = row["day0_date"]
            result.append(payload)
        return result

    def save_broker_snapshot(self, snapshot: BrokerSnapshot, actor: str = "broker_adapter") -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO broker_snapshots(id, event_id, broker, observed_at, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    snapshot.id,
                    snapshot.event_id,
                    snapshot.broker,
                    ensure_utc(snapshot.observed_at).isoformat(),
                    canonical_json(snapshot),
                ),
            )
            self.audit(
                actor,
                "broker_snapshot.created",
                "broker_snapshot",
                snapshot.id,
                {
                    "event_id": snapshot.event_id,
                    "broker": snapshot.broker,
                    "shortable": snapshot.shortable,
                    "available_shares": snapshot.available_shares,
                },
                connection=conn,
            )

    def latest_broker_payload(
        self, event_id: str, broker: str | None = None
    ) -> dict[str, Any] | None:
        if broker:
            row = self._connection.execute(
                """SELECT payload_json FROM broker_snapshots
                   WHERE event_id = ? AND broker = ? ORDER BY observed_at DESC LIMIT 1""",
                (event_id, broker),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT payload_json FROM broker_snapshots WHERE event_id = ? ORDER BY observed_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def save_decision(self, decision: CandidateDecision, actor: str = "decision_engine") -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO candidate_decisions(
                    id, event_id, feature_snapshot_id, created_at, strategy,
                    score, band, status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.id,
                    decision.event_id,
                    decision.feature_snapshot_id,
                    ensure_utc(decision.created_at).isoformat(),
                    decision.strategy.value,
                    decision.score,
                    decision.band,
                    decision.status.value,
                    canonical_json(decision),
                ),
            )
            conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (decision.status.value, utc_now().isoformat(), decision.event_id),
            )
            self.audit(
                actor,
                "candidate_decision.created",
                "candidate_decision",
                decision.id,
                {
                    "event_id": decision.event_id,
                    "score": decision.score,
                    "band": decision.band,
                    "status": decision.status.value,
                },
                connection=conn,
            )

    def get_decision_payload(self, decision_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json, status FROM candidate_decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        return payload

    def list_decision_payloads(
        self, status: EventStatus | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if status:
            rows = self._connection.execute(
                """SELECT d.payload_json, d.status, e.symbol, e.exchange, e.day0_date
                   FROM candidate_decisions d JOIN events e ON e.id = d.event_id
                   WHERE d.status = ? ORDER BY d.created_at DESC LIMIT ?""",
                (status.value, limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """SELECT d.payload_json, d.status, e.symbol, e.exchange, e.day0_date
                   FROM candidate_decisions d JOIN events e ON e.id = d.event_id
                   ORDER BY d.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.update(
                {
                    "status": row["status"],
                    "symbol": row["symbol"],
                    "exchange": row["exchange"],
                    "day0_date": row["day0_date"],
                }
            )
            result.append(payload)
        return result

    def record_approval(self, approval: Approval) -> None:
        if approval.action not in {"approve", "reject"}:
            raise ValueError("approval action must be approve or reject")
        target_status = (
            EventStatus.APPROVED if approval.action == "approve" else EventStatus.REJECTED
        )
        with self.transaction() as conn:
            decision = conn.execute(
                "SELECT event_id, status, payload_json FROM candidate_decisions WHERE id = ?",
                (approval.decision_id,),
            ).fetchone()
            if not decision:
                raise KeyError(f"decision not found: {approval.decision_id}")
            allowed_statuses = (
                {EventStatus.ALERTED.value}
                if approval.action == "approve"
                else {EventStatus.ALERTED.value, EventStatus.APPROVED.value}
            )
            if decision["status"] not in allowed_statuses:
                raise ValueError(f"decision cannot transition from {decision['status']}")
            conn.execute(
                """INSERT INTO approvals(decision_id, operator, action, reason, occurred_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    approval.decision_id,
                    approval.operator,
                    approval.action,
                    approval.reason,
                    ensure_utc(approval.occurred_at).isoformat(),
                ),
            )
            conn.execute(
                "UPDATE candidate_decisions SET status = ? WHERE id = ?",
                (target_status.value, approval.decision_id),
            )
            conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (target_status.value, utc_now().isoformat(), decision["event_id"]),
            )
            self.audit(
                approval.operator,
                f"candidate.{approval.action}",
                "candidate_decision",
                approval.decision_id,
                {"reason": approval.reason, "target_status": target_status.value},
                connection=conn,
                occurred_at=approval.occurred_at,
            )

    def update_decision_status(
        self,
        decision_id: str,
        status: EventStatus,
        actor: str,
        reason: str,
    ) -> None:
        with self.transaction() as conn:
            decision = conn.execute(
                "SELECT event_id, status FROM candidate_decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            if not decision:
                raise KeyError(f"decision not found: {decision_id}")
            conn.execute(
                "UPDATE candidate_decisions SET status = ? WHERE id = ?",
                (status.value, decision_id),
            )
            conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, utc_now().isoformat(), decision["event_id"]),
            )
            self.audit(
                actor,
                "candidate.status_changed",
                "candidate_decision",
                decision_id,
                {"from": decision["status"], "to": status.value, "reason": reason},
                connection=conn,
            )

    def save_order(
        self,
        order_id: str,
        decision_id: str,
        event_id: str,
        mode: str,
        status: str,
        payload: Mapping[str, Any],
        actor: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO orders(id, decision_id, event_id, mode, status, created_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    decision_id,
                    event_id,
                    mode,
                    status,
                    utc_now().isoformat(),
                    canonical_json(payload),
                ),
            )
            self.audit(
                actor,
                "order.created",
                "order",
                order_id,
                {"decision_id": decision_id, "mode": mode, "status": status},
                connection=conn,
            )

    def count_open_orders_for_event(self, event_id: str) -> int:
        row = self._connection.execute(
            """SELECT COUNT(*) AS count FROM orders
               WHERE event_id = ? AND status IN ('accepted', 'partially_filled', 'filled')""",
            (event_id,),
        ).fetchone()
        return int(row["count"])

    def save_backtest(self, result: BacktestResult, actor: str = "backtest") -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO backtest_runs(
                    id, created_at, strategy, stress_label, total_pnl, trade_count, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.id,
                    ensure_utc(result.created_at).isoformat(),
                    result.strategy.value,
                    result.stress_label,
                    result.metrics.total_pnl,
                    result.metrics.trades,
                    canonical_json(result),
                ),
            )
            self.audit(
                actor,
                "backtest.completed",
                "backtest_run",
                result.id,
                {
                    "strategy": result.strategy.value,
                    "stress_label": result.stress_label,
                    "trades": result.metrics.trades,
                    "total_pnl": result.metrics.total_pnl,
                },
                connection=conn,
            )

    def latest_backtest_payloads(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload_json FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def dashboard_summary(self) -> dict[str, Any]:
        event_counts = {
            row["status"]: int(row["count"])
            for row in self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
        }
        decision = self._connection.execute(
            """SELECT COUNT(*) AS count, AVG(score) AS average_score,
                      SUM(CASE WHEN json_extract(payload_json, '$.execution.executable') = 1 THEN 1 ELSE 0 END) AS executable
               FROM candidate_decisions"""
        ).fetchone()
        latest_observation = self._connection.execute(
            "SELECT MAX(observed_at) AS latest FROM observations"
        ).fetchone()["latest"]
        audit_ok, bad_sequence = self.verify_audit_chain()
        return {
            "events_by_status": event_counts,
            "candidate_count": int(decision["count"] or 0),
            "average_score": float(decision["average_score"] or 0.0),
            "executable_count": int(decision["executable"] or 0),
            "latest_observation_at": latest_observation,
            "audit_chain_valid": audit_ok,
            "audit_bad_sequence": bad_sequence,
        }

    def audit_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM audit_log ORDER BY sequence DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) | {"details": json.loads(row["details_json"])} for row in rows]

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EventCase:
        return EventCase(
            id=row["id"],
            symbol=row["symbol"],
            exchange=row["exchange"],
            day0_date=date.fromisoformat(row["day0_date"]),
            detected_at=parse_datetime(row["detected_at"]),
            status=EventStatus(row["status"]),
            baseline_close=float(row["baseline_close"]),
            detection_price=float(row["detection_price"]),
            source=row["source"],
        )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> Observation:
        return Observation(
            id=row["id"],
            event_id=row["event_id"],
            symbol=row["symbol"],
            kind=row["kind"],
            effective_at=parse_datetime(row["effective_at"]),
            observed_at=parse_datetime(row["observed_at"]),
            source=row["source"],
            source_ref=row["source_ref"],
            payload=json.loads(row["payload_json"]),
        )
