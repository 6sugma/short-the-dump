from __future__ import annotations

from datetime import UTC, datetime, timedelta

from short_the_dump.models import EventStatus, Observation
from short_the_dump.store import EventStore


def test_point_in_time_reads_exclude_late_corrections(tmp_path, event) -> None:
    with EventStore(tmp_path / "pit.db") as store:
        store.create_event(event)
        effective = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
        first = Observation.create(
            symbol=event.symbol,
            event_id=event.id,
            kind="market_snapshot",
            effective_at=effective,
            observed_at=effective + timedelta(minutes=1),
            source="vendor",
            source_ref="quote-v1",
            payload={"price": 4.0},
        )
        correction = Observation.create(
            symbol=event.symbol,
            event_id=event.id,
            kind="market_snapshot",
            effective_at=effective,
            observed_at=effective + timedelta(hours=2),
            source="vendor",
            source_ref="quote-correction",
            payload={"price": 4.2},
        )
        assert store.append_observation(first)
        assert store.append_observation(correction)
        early = store.observations_as_of(event.symbol, effective + timedelta(minutes=30))
        late = store.observations_as_of(event.symbol, effective + timedelta(hours=3))
        assert [item.payload["price"] for item in early] == [4.0]
        assert [item.payload["price"] for item in late] == [4.0, 4.2]


def test_observation_ingest_is_idempotent(tmp_path, event) -> None:
    with EventStore(tmp_path / "idempotent.db") as store:
        store.create_event(event)
        item = Observation.create(
            symbol=event.symbol,
            event_id=event.id,
            kind="news",
            effective_at=event.detected_at,
            observed_at=event.detected_at,
            source="wire",
            source_ref="wire://1",
            payload={"headline": "Test"},
        )
        assert store.append_observation(item)
        duplicate = Observation.create(
            symbol=event.symbol,
            event_id=event.id,
            kind="news",
            effective_at=event.detected_at,
            observed_at=event.detected_at,
            source="wire",
            source_ref="wire://1",
            payload={"headline": "Test"},
        )
        assert store.append_observation(duplicate) is False


def test_audit_chain_and_status_transition(tmp_path, event) -> None:
    with EventStore(tmp_path / "audit.db") as store:
        store.create_event(event)
        store.update_event_status(event.id, EventStatus.ALERTED, "tester", "test alert")
        valid, sequence = store.verify_audit_chain()
        assert valid is True
        assert sequence is None
        assert store.get_event(event.id).status is EventStatus.ALERTED
        assert len(store.audit_rows()) == 2
