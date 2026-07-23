from __future__ import annotations

import dataclasses

from short_the_dump.config import ExecutionConfig, RiskConfig
from short_the_dump.execution import ExecutionGate
from short_the_dump.models import (
    CatalystAssessment,
    CatalystLabel,
    FeatureSnapshot,
    RiskSnapshot,
    SupplyRiskAssessment,
)
from short_the_dump.risk import RiskEngine


def feature(event) -> FeatureSnapshot:
    return FeatureSnapshot.create(
        event_id=event.id,
        as_of=event.detected_at,
        price=4.0,
        day0_return=1.0,
        relative_volume=12.0,
        dollar_volume=20_000_000,
        float_shares=3_000_000,
        float_turnover=4.0,
        market_cap=40_000_000,
        vwap_distance=-0.1,
        close_off_high=0.3,
        lower_high_ratio=0.8,
        failed_vwap_reclaims=2,
        opening_range_breakdown=True,
        volume_fade_ratio=0.4,
        halt_count=0,
        social_attention_zscore=4.0,
        catalyst=CatalystAssessment(CatalystLabel.LOW_MATERIALITY, 0.8, 0.7, 0.2, 0.1, 0.8),
        supply_risk=SupplyRiskAssessment(0.7, True, True, True, False, 3.0),
        momentum_failure_score=80,
        data_freshness_seconds=0,
    )


def test_execution_gate_passes_and_sizes_to_real_constraints(event, broker) -> None:
    decision = ExecutionGate(ExecutionConfig()).evaluate(
        feature(event),
        broker,
        5_000,
        event.detected_at,
        average_bar_dollar_volume=200_000,
    )
    assert decision.executable is True
    assert 100 <= decision.executable_quantity < 5_000
    assert all(check.passed for check in decision.checks)


def test_execution_gate_rejects_stale_inventory_and_rule201(event, broker) -> None:
    unsafe = dataclasses.replace(
        broker,
        observed_at=event.detected_at.replace(hour=14),
        rule201_active=True,
        price_test_supported=False,
    )
    decision = ExecutionGate(ExecutionConfig()).evaluate(
        feature(event),
        unsafe,
        1_000,
        event.detected_at,
        average_bar_dollar_volume=1_000_000,
    )
    assert decision.executable is False
    assert {check.name for check in decision.checks if not check.passed} >= {
        "inventory_freshness",
        "rule201_price_test",
    }


def test_execution_gate_rejects_stale_market_feature(event, broker) -> None:
    stale_feature = dataclasses.replace(feature(event), data_freshness_seconds=180)
    decision = ExecutionGate(ExecutionConfig()).evaluate(
        stale_feature,
        broker,
        1_000,
        event.detected_at,
        average_bar_dollar_volume=1_000_000,
    )
    assert decision.executable is False
    assert "market_data_freshness" in {check.name for check in decision.checks if not check.passed}


def test_risk_engine_reserves_for_halt_gap(event, broker) -> None:
    execution = ExecutionGate(ExecutionConfig()).evaluate(
        feature(event),
        broker,
        1_000,
        event.detected_at,
        average_bar_dollar_volume=1_000_000,
    )
    risk = RiskSnapshot(event.detected_at, 100_000, 0, 0, 0, 0)
    plan = RiskEngine(RiskConfig()).plan_short(4.0, 5.0, 3.0, risk, broker, execution)
    assert plan.accepted is True
    assert plan.quantity > 0
    assert plan.worst_case_loss <= plan.risk_budget
    assert plan.quantity < 250  # gap reserve makes size smaller than stop-only sizing


def test_risk_engine_blocks_daily_kill_and_averaging_loser(event, broker) -> None:
    execution = ExecutionGate(ExecutionConfig()).evaluate(
        feature(event),
        broker,
        1_000,
        event.detected_at,
        average_bar_dollar_volume=1_000_000,
    )
    losing = RiskSnapshot(
        event.detected_at,
        100_000,
        -1_100,
        0,
        2_000,
        1,
        symbol_position_quantity=100,
        symbol_average_price=4.0,
        symbol_unrealized_pnl=-100,
    )
    plan = RiskEngine(RiskConfig()).plan_short(4.0, 5.0, 3.0, losing, broker, execution)
    assert plan.accepted is False
    assert any("kill switch" in reason for reason in plan.reasons)
    assert any("losing short" in reason for reason in plan.reasons)
