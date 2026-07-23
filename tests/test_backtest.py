from __future__ import annotations

import dataclasses
from datetime import timedelta

from conftest import make_replay

from short_the_dump.backtest import (
    BacktestEngine,
    SimulationParameters,
    StressScenario,
    WalkForwardOptimizer,
)
from short_the_dump.config import BacktestConfig
from short_the_dump.models import ExitReason, StrategyKind


def test_backtest_models_costs_and_partial_fills(event) -> None:
    replay = make_replay(event, volume=1_000)
    engine = BacktestEngine(BacktestConfig(max_volume_participation=0.05))
    result = engine.run(
        [replay],
        SimulationParameters(strategy=StrategyKind.SAME_DAY, max_holding_bars=20),
    )
    assert result.metrics.trades == 1
    trade = result.trades[0]
    assert trade.partial_fill is True
    assert trade.locate_cost > 0
    assert trade.borrow_cost >= 0
    assert trade.slippage_cost > 0


def test_recall_forces_cover(event) -> None:
    replay = make_replay(event, recalled=True)
    engine = BacktestEngine(BacktestConfig(recall_cover_delay_bars=1))
    result = engine.run(
        [replay],
        SimulationParameters(strategy=StrategyKind.DAY_PLUS_1, max_holding_bars=40),
    )
    assert result.metrics.trades == 1
    assert result.trades[0].exit_reason is ExitReason.RECALL
    assert result.trades[0].forced_action is True


def test_forced_buy_in_stress_worsens_or_accelerates_exit(event) -> None:
    replay = make_replay(event)
    engine = BacktestEngine(BacktestConfig())
    parameters = SimulationParameters(strategy=StrategyKind.SAME_DAY, max_holding_bars=40)
    base = engine.run([replay], parameters)
    stress = engine.run(
        [replay],
        parameters,
        StressScenario(
            label="stress",
            slippage_multiplier=2,
            volume_haircut=0.25,
            borrow_fee_multiplier=2,
            force_buy_in_after_bars=5,
        ),
    )
    assert stress.trades[0].forced_action is True
    assert stress.trades[0].exit_at <= base.trades[0].exit_at


def test_all_entry_strategy_variants_run(event) -> None:
    replay = make_replay(event)
    results = BacktestEngine(BacktestConfig()).compare_strategies(
        [replay], SimulationParameters(max_holding_bars=10, opening_range_bars=3)
    )
    assert set(results) == set(StrategyKind)
    assert results[StrategyKind.SAME_DAY].metrics.trades == 1
    assert results[StrategyKind.DAY_PLUS_1].metrics.trades == 1
    assert results[StrategyKind.DAY_PLUS_2].metrics.trades == 1
    assert results[StrategyKind.FIRST_RED_DAY].metrics.trades == 1


def test_walk_forward_reports_oos_and_ranges(event) -> None:
    replays = []
    for index in range(6):
        # Build distinct chronological copies without depending on market calendars.
        shifted = dataclasses.replace(
            event,
            id=f"event-{index}",
            day0_date=event.day0_date + timedelta(days=index * 3),
            detected_at=event.detected_at + timedelta(days=index * 3),
        )
        replays.append(make_replay(shifted))
    grid = [
        SimulationParameters(
            strategy=StrategyKind.SAME_DAY, stop_loss_fraction=stop, max_holding_bars=hold
        )
        for stop in (0.2, 0.3)
        for hold in (10, 20)
    ]
    result = WalkForwardOptimizer(BacktestEngine(BacktestConfig())).evaluate(
        replays, grid, train_events=3, test_events=1
    )
    assert result["windows"]
    assert result["oos_metrics"]["trades"] > 0
    assert result["robust_ranges"]["stop_loss_fraction"]["support"] > 0
