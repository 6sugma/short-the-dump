from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import AppConfig
from .execution import ExecutionGate
from .features import ExtremeMoveDetector, FeatureEngine, cosine_like_similarity
from .models import (
    BrokerSnapshot,
    EventCase,
    ExecutionDecision,
    GateCheck,
    Observation,
    RiskSnapshot,
    StrategyKind,
)
from .risk import RiskEngine
from .scoring import DecisionEngine
from .store import EventStore


class ResearchService:
    def __init__(self, config: AppConfig, store: EventStore) -> None:
        self.config = config
        self.store = store
        self.detector = ExtremeMoveDetector(config.detection)
        self.features = FeatureEngine(config.signals)
        self.execution = ExecutionGate(config.execution)
        self.risk = RiskEngine(config.risk)
        self.decisions = DecisionEngine(config.signals, self.features)

    def ingest(self, observation: Observation) -> bool:
        return self.store.append_observation(observation)

    def detect(self, observation: Observation) -> tuple[EventCase | None, tuple[str, ...]]:
        if observation.kind != "market_snapshot":
            raise ValueError("detection requires a market_snapshot observation")
        qualifies, reasons = self.detector.evaluate(observation.payload)
        if not qualifies:
            return None, reasons
        event = EventCase.create(
            symbol=observation.symbol,
            exchange=str(observation.payload["exchange"]),
            day0_date=observation.effective_at.date(),
            detected_at=observation.observed_at,
            baseline_close=float(observation.payload["previous_close"]),
            detection_price=float(observation.payload["price"]),
            source=observation.source,
        )
        return self.store.create_event(event), ()

    def analyze(
        self,
        event_id: str,
        as_of: datetime,
        *,
        broker: BrokerSnapshot | None,
        risk_snapshot: RiskSnapshot,
        strategy: StrategyKind = StrategyKind.DAY_PLUS_1,
        requested_quantity: int = 1_000,
        stop_fraction: float = 0.25,
        target_fraction: float | None = 0.30,
    ) -> Any:
        event = self.store.get_event(event_id)
        if event is None:
            raise KeyError(f"event not found: {event_id}")
        observations = self.store.observations_as_of(event.symbol, as_of, event_id=event_id)
        feature = self.features.build(event, observations, as_of)
        self.store.save_feature_snapshot(feature)
        latest_market = max(
            (item for item in observations if item.kind == "market_snapshot"),
            key=lambda item: item.observed_at,
        )
        average_bar_dollar_volume = float(
            latest_market.payload.get("average_bar_dollar_volume", feature.dollar_volume / 390)
        )
        latest_bar = max(
            (item for item in observations if item.kind == "bar"),
            key=lambda item: item.effective_at,
            default=None,
        )
        currently_halted = bool(latest_bar and latest_bar.payload.get("halted"))
        if broker is None:
            execution = ExecutionDecision(
                executable=False,
                requested_quantity=requested_quantity,
                executable_quantity=0,
                estimated_locate_cost=0.0,
                estimated_initial_margin=0.0,
                checks=(
                    GateCheck(
                        name="broker_inventory",
                        passed=False,
                        measured=None,
                        limit="fresh broker-specific snapshot",
                        reason="no broker-specific short inventory snapshot was supplied",
                    ),
                ),
            )
            plan = None
        else:
            if broker.event_id != event_id:
                raise ValueError("broker snapshot belongs to a different event")
            self.store.save_broker_snapshot(broker)
            execution = self.execution.evaluate(
                feature,
                broker,
                requested_quantity,
                as_of,
                average_bar_dollar_volume=average_bar_dollar_volume,
                currently_halted=currently_halted,
            )
            entry = broker.bid if broker.bid > 0 else feature.price
            stop = entry * (1 + stop_fraction)
            target = entry * (1 - target_fraction) if target_fraction is not None else None
            plan = self.risk.plan_short(entry, stop, target, risk_snapshot, broker, execution)
        decision = self.decisions.decide(feature, strategy, as_of, execution, plan)
        self.store.save_decision(decision)
        return decision

    def analogs(self, event_id: str, limit: int = 5) -> list[dict[str, Any]]:
        target = self.store.latest_feature_payload(event_id)
        if target is None:
            return []
        candidates = [
            item for item in self.store.feature_payloads() if item.get("event_id") != event_id
        ]
        ranked = sorted(
            (
                {
                    "event_id": item.get("event_id"),
                    "symbol": item.get("symbol"),
                    "day0_date": item.get("day0_date"),
                    "similarity": cosine_like_similarity(target, item),
                    "day0_return": item.get("day0_return"),
                    "relative_volume": item.get("relative_volume"),
                    "float_turnover": item.get("float_turnover"),
                    "momentum_failure_score": item.get("momentum_failure_score"),
                }
                for item in candidates
            ),
            key=lambda item: item["similarity"],
            reverse=True,
        )
        return ranked[:limit]
