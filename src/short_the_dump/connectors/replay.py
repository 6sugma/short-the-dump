from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import BrokerSnapshot, Observation, parse_datetime


class JsonlObservationProvider:
    """Deterministic replay adapter for captured vendor data and public fixtures."""

    name = "jsonl-replay"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def observations(self) -> list[Observation]:
        result: list[Observation] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                try:
                    result.append(
                        Observation.create(
                            symbol=str(raw["symbol"]),
                            kind=str(raw["kind"]),
                            effective_at=parse_datetime(raw["effective_at"]),
                            observed_at=parse_datetime(raw["observed_at"]),
                            source=str(raw.get("source", self.name)),
                            payload=dict(raw["payload"]),
                            event_id=raw.get("event_id"),
                            source_ref=raw.get("source_ref"),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid observation on line {line_number}: {exc}") from exc
        return result


class ReplayBrokerInventoryProvider:
    name = "broker-replay"

    def __init__(self, snapshots: Mapping[str, Mapping[str, Any]]) -> None:
        self.snapshots = snapshots

    def inventory(self, event_id: str, symbol: str, as_of: datetime) -> BrokerSnapshot:
        raw = dict(self.snapshots[symbol.upper()])
        raw.update(event_id=event_id, observed_at=as_of)
        return BrokerSnapshot.create(**raw)
