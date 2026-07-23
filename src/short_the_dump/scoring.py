from __future__ import annotations

from datetime import datetime

from .config import SignalConfig
from .features import FeatureEngine
from .models import (
    CandidateDecision,
    EventStatus,
    ExecutionDecision,
    FeatureSnapshot,
    PositionPlan,
    StrategyKind,
    ensure_utc,
)


class DecisionEngine:
    def __init__(self, signals: SignalConfig, feature_engine: FeatureEngine) -> None:
        self.signals = signals
        self.feature_engine = feature_engine

    def decide(
        self,
        snapshot: FeatureSnapshot,
        strategy: StrategyKind,
        created_at: datetime,
        execution: ExecutionDecision | None = None,
        position_plan: PositionPlan | None = None,
    ) -> CandidateDecision:
        score = self.feature_engine.reversal_score(snapshot)
        risk_flags: list[str] = []
        if snapshot.halt_count:
            risk_flags.append("halt_gap_risk")
        if snapshot.social_attention_zscore and snapshot.social_attention_zscore >= 5:
            risk_flags.append("crowded_attention")
        if snapshot.float_turnover and snapshot.float_turnover >= 5:
            risk_flags.append("extreme_float_turnover_data_quality_and_squeeze_risk")
        if snapshot.catalyst.label.value == "material_novel":
            risk_flags.append("material_novel_catalyst_against_short")
        if snapshot.data_freshness_seconds > 90:
            risk_flags.append("stale_market_snapshot")
        if execution and not execution.executable:
            risk_flags.extend(f"execution:{reason}" for reason in execution.rejection_reasons)
        if position_plan and not position_plan.accepted:
            risk_flags.extend(f"risk:{reason}" for reason in position_plan.reasons)

        if score >= self.signals.high_conviction_score:
            band = "strong_failure_evidence"
        elif score >= self.signals.alert_score:
            band = "failure_evidence"
        elif score >= 45:
            band = "mixed"
        else:
            band = "momentum_intact_or_insufficient"

        actionable_signal = (
            score >= self.signals.alert_score
            and snapshot.catalyst.confidence >= self.signals.minimum_catalyst_confidence
        )
        status = EventStatus.ALERTED if actionable_signal else EventStatus.WATCH
        if actionable_signal and execution is not None and not execution.executable:
            # Still retain an alert for research, but make non-executability explicit.
            thesis = (
                f"{band.replace('_', ' ')} at {score:.1f}/100, but the broker-specific "
                "execution gate rejected the trade."
            )
        elif actionable_signal:
            thesis = (
                f"{band.replace('_', ' ')} at {score:.1f}/100. This is an explainable "
                "research rating, not a calibrated probability."
            )
        else:
            thesis = (
                f"No alert: evidence rating {score:.1f}/100 is below the configured "
                f"{self.signals.alert_score:.1f} threshold or catalyst confidence is insufficient."
            )
        return CandidateDecision.create(
            event_id=snapshot.event_id,
            feature_snapshot_id=snapshot.id,
            created_at=ensure_utc(created_at),
            strategy=strategy,
            score=score,
            band=band,
            status=status,
            thesis=thesis,
            evidence=snapshot.evidence,
            risk_flags=tuple(dict.fromkeys(risk_flags)),
            execution=execution,
            position_plan=position_plan,
        )
