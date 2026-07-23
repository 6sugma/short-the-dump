from __future__ import annotations

import dataclasses
import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import BacktestConfig
from .models import (
    BacktestMetrics,
    BacktestResult,
    BacktestTrade,
    BorrowPoint,
    EventReplay,
    ExitReason,
    MarketBar,
    SimFill,
    StrategyKind,
    clamp,
    ensure_utc,
)


@dataclass(frozen=True, slots=True)
class SimulationParameters:
    strategy: StrategyKind = StrategyKind.DAY_PLUS_1
    stop_loss_fraction: float = 0.25
    profit_target_fraction: float | None = 0.30
    max_holding_bars: int = 390
    entry_delay_bars: int = 0
    entry_fill_window_bars: int = 5
    opening_range_bars: int = 15
    limit_offset_bps: float = 0.0

    def validate(self) -> SimulationParameters:
        if not 0 < self.stop_loss_fraction <= 2:
            raise ValueError("stop_loss_fraction must be in (0, 2]")
        if self.profit_target_fraction is not None and not 0 < self.profit_target_fraction < 1:
            raise ValueError("profit_target_fraction must be in (0, 1)")
        if self.max_holding_bars <= 0 or self.entry_fill_window_bars <= 0:
            raise ValueError("holding and fill windows must be positive")
        return self


@dataclass(frozen=True, slots=True)
class StressScenario:
    label: str = "base"
    slippage_multiplier: float = 1.0
    volume_haircut: float = 0.0
    borrow_fee_multiplier: float = 1.0
    force_buy_in_after_bars: int | None = None
    random_forced_buy_in_probability: float = 0.0
    seed: int = 7

    def validate(self) -> StressScenario:
        if self.slippage_multiplier < 1:
            raise ValueError("stress slippage multiplier cannot improve fills")
        if not 0 <= self.volume_haircut < 1:
            raise ValueError("volume_haircut must be in [0, 1)")
        if self.borrow_fee_multiplier < 1:
            raise ValueError("stress borrow multiplier cannot improve costs")
        if not 0 <= self.random_forced_buy_in_probability <= 1:
            raise ValueError("forced-buy-in probability must be in [0, 1]")
        return self


class BacktestEngine:
    """Event-driven, borrow-aware simulator with pessimistic microstructure assumptions."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    def run(
        self,
        replays: Sequence[EventReplay],
        parameters: SimulationParameters,
        stress: StressScenario | None = None,
    ) -> BacktestResult:
        parameters.validate()
        stress = (stress or StressScenario()).validate()
        trades: list[BacktestTrade] = []
        for replay in sorted(replays, key=lambda item: item.event.detected_at):
            trade = self.simulate_event(replay, parameters, stress)
            if trade is not None:
                trades.append(trade)
        return BacktestResult.create(
            strategy=parameters.strategy,
            parameters=dataclasses.asdict(parameters),
            metrics=calculate_metrics(trades),
            trades=tuple(trades),
            stress_label=stress.label,
        )

    def compare_strategies(
        self,
        replays: Sequence[EventReplay],
        base: SimulationParameters,
        stress: StressScenario | None = None,
    ) -> dict[StrategyKind, BacktestResult]:
        return {
            strategy: self.run(replays, dataclasses.replace(base, strategy=strategy), stress)
            for strategy in StrategyKind
        }

    def simulate_event(
        self,
        replay: EventReplay,
        parameters: SimulationParameters,
        stress: StressScenario,
    ) -> BacktestTrade | None:
        bars = sorted(replay.bars, key=lambda item: item.timestamp)
        if not bars or replay.desired_quantity <= 0:
            return None
        entry_index = self._entry_index(replay, bars, parameters)
        if entry_index is None:
            return None
        borrow_at_entry = self._borrow_at(replay.borrow, bars[entry_index].timestamp)
        if (
            borrow_at_entry is None
            or borrow_at_entry.recalled
            or borrow_at_entry.available_shares <= 0
        ):
            return None
        desired = min(replay.desired_quantity, borrow_at_entry.available_shares)
        entry_reference = bars[entry_index].open
        limit_price = entry_reference * (1 + parameters.limit_offset_bps / 10_000)
        entry_fills: list[SimFill] = []
        remaining = desired
        last_entry_index = entry_index
        for index in range(
            entry_index, min(len(bars), entry_index + parameters.entry_fill_window_bars)
        ):
            bar = bars[index]
            if bar.halted or bar.volume <= 0:
                continue
            current_borrow = self._borrow_at(replay.borrow, bar.timestamp)
            if current_borrow is None or current_borrow.recalled:
                break
            available = max(
                current_borrow.available_shares - sum(fill.quantity for fill in entry_fills), 0
            )
            capacity = self._bar_capacity(bar, stress)
            quantity = min(remaining, available, capacity)
            if quantity <= 0:
                continue
            if parameters.limit_offset_bps > 0 and bar.high < limit_price:
                continue
            if replay.rule201_active:
                # With bar data, require an observable uptick above the opening proxy for NBB.
                price_test = bar.open * 1.0001
                if bar.high < price_test:
                    continue
                reference = max(bar.open, price_test)
            else:
                reference = bar.open
            participation = quantity / max(bar.volume, 1)
            slippage_bps = self._slippage_bps(bar, participation, stress)
            fill_price = max(reference * (1 - slippage_bps / 10_000), bar.low)
            entry_fills.append(
                SimFill(
                    order_id=f"entry_{replay.event.id}",
                    timestamp=bar.timestamp,
                    quantity=quantity,
                    price=fill_price,
                    slippage_bps=slippage_bps,
                )
            )
            remaining -= quantity
            last_entry_index = index
            if remaining <= 0:
                break
        if not entry_fills:
            return None
        filled_quantity = sum(fill.quantity for fill in entry_fills)
        average_entry = sum(fill.quantity * fill.price for fill in entry_fills) / filled_quantity
        stop_price = average_entry * (1 + parameters.stop_loss_fraction)
        target_price = (
            average_entry * (1 - parameters.profit_target_fraction)
            if parameters.profit_target_fraction is not None
            else None
        )

        exit_trigger_index: int | None = None
        exit_reason = ExitReason.DATA_END
        forced_action = False
        recall_seen_index: int | None = None
        randomizer = random.Random(f"{stress.seed}:{replay.event.id}")
        force_random = randomizer.random() < stress.random_forced_buy_in_probability
        random_force_bar = (
            last_entry_index + max(1, parameters.max_holding_bars // 2) if force_random else None
        )
        for held_bars, index in enumerate(range(last_entry_index + 1, len(bars)), start=1):
            bar = bars[index]
            if bar.halted:
                continue
            borrow = self._borrow_at(replay.borrow, bar.timestamp)
            if borrow and borrow.recalled and recall_seen_index is None:
                recall_seen_index = index
            if (
                recall_seen_index is not None
                and index - recall_seen_index >= self.config.recall_cover_delay_bars
            ):
                exit_trigger_index = index
                exit_reason = ExitReason.RECALL
                forced_action = True
                break
            forced_after = stress.force_buy_in_after_bars
            if forced_after is not None and held_bars >= forced_after:
                exit_trigger_index = index
                exit_reason = ExitReason.FORCED_BUY_IN
                forced_action = True
                break
            if random_force_bar is not None and index >= random_force_bar:
                exit_trigger_index = index
                exit_reason = ExitReason.FORCED_BUY_IN
                forced_action = True
                break
            if bar.high >= stop_price:
                exit_trigger_index = index
                exit_reason = ExitReason.STOP
                break
            if target_price is not None and bar.low <= target_price:
                exit_trigger_index = index
                exit_reason = ExitReason.TARGET
                break
            if held_bars >= parameters.max_holding_bars:
                exit_trigger_index = index
                exit_reason = ExitReason.MAX_HOLD
                break
        if exit_trigger_index is None:
            exit_trigger_index = len(bars) - 1
            while exit_trigger_index > last_entry_index and bars[exit_trigger_index].halted:
                exit_trigger_index -= 1
            if exit_trigger_index <= last_entry_index:
                return None

        exit_fills, actual_exit_index = self._cover(
            bars,
            exit_trigger_index,
            filled_quantity,
            exit_reason,
            stop_price,
            target_price,
            stress,
            replay.event.id,
        )
        if not exit_fills:
            return None
        covered = sum(fill.quantity for fill in exit_fills)
        if covered < filled_quantity:
            # End-of-data residual is marked at an adverse stressed price instead of disappearing.
            residual = filled_quantity - covered
            bar = bars[actual_exit_index]
            penalty_bps = self.config.forced_buy_in_slippage_bps * stress.slippage_multiplier
            exit_fills.append(
                SimFill(
                    order_id=f"exit_{replay.event.id}",
                    timestamp=bar.timestamp,
                    quantity=residual,
                    price=bar.high * (1 + penalty_bps / 10_000),
                    slippage_bps=penalty_bps,
                )
            )
            forced_action = True
            exit_reason = ExitReason.FORCED_BUY_IN
        average_exit = sum(fill.quantity * fill.price for fill in exit_fills) / filled_quantity
        entry_at = min(fill.timestamp for fill in entry_fills)
        exit_at = max(fill.timestamp for fill in exit_fills)
        locate_cost = filled_quantity * max(borrow_at_entry.locate_cost_per_share, 0)
        borrow_cost = self._borrow_cost(
            filled_quantity,
            average_entry,
            entry_at,
            exit_at,
            replay.borrow,
            stress,
        )
        gross_pnl = filled_quantity * (average_entry - average_exit)
        entry_slippage_cost = sum(
            fill.quantity * average_entry * fill.slippage_bps / 10_000 for fill in entry_fills
        )
        exit_slippage_cost = sum(
            fill.quantity * average_exit * fill.slippage_bps / 10_000 for fill in exit_fills
        )
        slippage_cost = entry_slippage_cost + exit_slippage_cost
        net_pnl = gross_pnl - locate_cost - borrow_cost
        notional = filled_quantity * average_entry
        return BacktestTrade(
            event_id=replay.event.id,
            symbol=replay.event.symbol,
            strategy=parameters.strategy,
            entry_at=entry_at,
            exit_at=exit_at,
            quantity=filled_quantity,
            average_entry=average_entry,
            average_exit=average_exit,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            return_on_notional=net_pnl / notional if notional else 0,
            locate_cost=locate_cost,
            borrow_cost=borrow_cost,
            slippage_cost=slippage_cost,
            exit_reason=exit_reason,
            partial_fill=filled_quantity < replay.desired_quantity,
            forced_action=forced_action,
        )

    def _entry_index(
        self,
        replay: EventReplay,
        bars: Sequence[MarketBar],
        parameters: SimulationParameters,
    ) -> int | None:
        session_dates = list(dict.fromkeys(bar.timestamp.date() for bar in bars))
        try:
            day0_index = session_dates.index(replay.event.day0_date)
        except ValueError:
            return None
        candidates: list[int] = []
        if parameters.strategy is StrategyKind.SAME_DAY:
            candidates = [
                index for index, bar in enumerate(bars) if bar.timestamp >= replay.signal_at
            ]
        elif parameters.strategy in {StrategyKind.DAY_PLUS_1, StrategyKind.DAY_PLUS_2}:
            offset = 1 if parameters.strategy is StrategyKind.DAY_PLUS_1 else 2
            if day0_index + offset >= len(session_dates):
                return None
            target_date = session_dates[day0_index + offset]
            candidates = [
                index for index, bar in enumerate(bars) if bar.timestamp.date() == target_date
            ]
        else:
            previous_close = self._session_close(bars, replay.event.day0_date)
            for session_date in session_dates[day0_index + 1 :]:
                indexes = [
                    index for index, bar in enumerate(bars) if bar.timestamp.date() == session_date
                ]
                tradable = [index for index in indexes if not bars[index].halted]
                for index in tradable[parameters.opening_range_bars :]:
                    if bars[index].close < previous_close:
                        candidates = [index]
                        break
                if candidates:
                    break
                if tradable:
                    previous_close = bars[tradable[-1]].close
        tradable_candidates = [index for index in candidates if not bars[index].halted]
        if parameters.entry_delay_bars >= len(tradable_candidates):
            return None
        return tradable_candidates[parameters.entry_delay_bars] if tradable_candidates else None

    def _cover(
        self,
        bars: Sequence[MarketBar],
        trigger_index: int,
        quantity: int,
        reason: ExitReason,
        stop_price: float,
        target_price: float | None,
        stress: StressScenario,
        event_id: str,
    ) -> tuple[list[SimFill], int]:
        fills: list[SimFill] = []
        remaining = quantity
        last_index = trigger_index
        for index in range(trigger_index, len(bars)):
            bar = bars[index]
            if bar.halted or bar.volume <= 0:
                continue
            capacity = self._bar_capacity(bar, stress)
            if reason in {ExitReason.RECALL, ExitReason.FORCED_BUY_IN, ExitReason.STOP}:
                capacity = max(
                    capacity,
                    math.floor(bar.volume * min(self.config.max_volume_participation * 2, 0.20)),
                )
            fill_quantity = min(remaining, capacity)
            if fill_quantity <= 0:
                continue
            if reason is ExitReason.STOP:
                reference = max(stop_price, bar.open)
            elif reason is ExitReason.TARGET and target_price is not None:
                reference = min(target_price, bar.open)
            else:
                reference = bar.open
            participation = fill_quantity / max(bar.volume, 1)
            slippage_bps = self._slippage_bps(bar, participation, stress)
            if reason in {ExitReason.RECALL, ExitReason.FORCED_BUY_IN}:
                slippage_bps += self.config.forced_buy_in_slippage_bps * stress.slippage_multiplier
            fill_price = max(reference * (1 + slippage_bps / 10_000), bar.low)
            fills.append(
                SimFill(
                    order_id=f"exit_{event_id}",
                    timestamp=bar.timestamp,
                    quantity=fill_quantity,
                    price=fill_price,
                    slippage_bps=slippage_bps,
                )
            )
            remaining -= fill_quantity
            last_index = index
            if remaining <= 0:
                break
        return fills, last_index

    def _bar_capacity(self, bar: MarketBar, stress: StressScenario) -> int:
        effective_volume = bar.volume * (1 - stress.volume_haircut)
        return max(math.floor(effective_volume * self.config.max_volume_participation), 0)

    def _slippage_bps(self, bar: MarketBar, participation: float, stress: StressScenario) -> float:
        spread_bps = bar.spread_bps
        participation_ratio = clamp(
            participation / max(self.config.max_volume_participation, 1e-9), 0, 2
        )
        base = (
            self.config.base_slippage_bps
            + spread_bps * self.config.spread_cross_fraction
            + self.config.impact_bps_at_max_participation * participation_ratio**2
        )
        return base * stress.slippage_multiplier

    @staticmethod
    def _borrow_at(points: Sequence[BorrowPoint], timestamp: datetime) -> BorrowPoint | None:
        eligible = [
            point for point in points if ensure_utc(point.timestamp) <= ensure_utc(timestamp)
        ]
        return max(eligible, key=lambda point: point.timestamp) if eligible else None

    def _borrow_cost(
        self,
        quantity: int,
        entry_price: float,
        entry_at: datetime,
        exit_at: datetime,
        points: Sequence[BorrowPoint],
        stress: StressScenario,
    ) -> float:
        boundaries = sorted(
            {ensure_utc(entry_at), ensure_utc(exit_at)}
            | {
                ensure_utc(point.timestamp)
                for point in points
                if ensure_utc(entry_at) < ensure_utc(point.timestamp) < ensure_utc(exit_at)
            }
        )
        cost = 0.0
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            point = self._borrow_at(points, start)
            if point is None:
                continue
            hours = max((end - start).total_seconds(), 0) / 3600
            annualized = max(point.fee_annualized, 0) * stress.borrow_fee_multiplier
            cost += quantity * entry_price * annualized * hours / (360 * 24)
        return cost

    @staticmethod
    def _session_close(bars: Sequence[MarketBar], session_date: Any) -> float:
        values = [
            bar.close for bar in bars if bar.timestamp.date() == session_date and not bar.halted
        ]
        return values[-1] if values else 0.0


def calculate_metrics(trades: Sequence[BacktestTrade]) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, 0.0, 0.0)
    pnls = [trade.net_pnl for trade in trades]
    returns = [trade.return_on_notional for trade in trades]
    wins = sum(value > 0 for value in pnls)
    losses = sum(value <= 0 for value in pnls)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gross_wins = sum(value for value in pnls if value > 0)
    gross_losses = -sum(value for value in pnls if value < 0)
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else None
    return BacktestMetrics(
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=wins / len(trades),
        total_pnl=sum(pnls),
        mean_pnl=statistics.fmean(pnls),
        median_pnl=statistics.median(pnls),
        mean_return=statistics.fmean(returns),
        max_drawdown=max_drawdown,
        profit_factor=profit_factor,
        forced_exit_rate=sum(trade.forced_action for trade in trades) / len(trades),
        partial_fill_rate=sum(trade.partial_fill for trade in trades) / len(trades),
    )


class WalkForwardOptimizer:
    """Selects train-set plateaus and reports untouched chronological test performance."""

    def __init__(self, engine: BacktestEngine) -> None:
        self.engine = engine

    def evaluate(
        self,
        replays: Sequence[EventReplay],
        grid: Sequence[SimulationParameters],
        *,
        train_events: int,
        test_events: int,
        top_plateau_fraction: float = 0.15,
        stress: StressScenario | None = None,
    ) -> dict[str, Any]:
        if train_events < 2 or test_events < 1:
            raise ValueError("walk-forward windows require at least 2 train and 1 test event")
        if not grid:
            raise ValueError("parameter grid is empty")
        ordered = sorted(replays, key=lambda item: item.event.detected_at)
        windows: list[dict[str, Any]] = []
        plateau_parameters: list[SimulationParameters] = []
        all_oos_trades: list[BacktestTrade] = []
        start = 0
        while start + train_events + test_events <= len(ordered):
            train = ordered[start : start + train_events]
            test = ordered[start + train_events : start + train_events + test_events]
            ranked: list[tuple[float, SimulationParameters, BacktestResult]] = []
            for parameters in grid:
                result = self.engine.run(train, parameters, stress)
                objective = self._objective(result.metrics)
                ranked.append((objective, parameters, result))
            ranked.sort(key=lambda item: item[0], reverse=True)
            best_score = ranked[0][0]
            tolerance = max(abs(best_score) * top_plateau_fraction, 1e-9)
            plateau = [item for item in ranked if item[0] >= best_score - tolerance]
            selected = self._central_parameter([item[1] for item in plateau])
            oos = self.engine.run(test, selected, stress)
            all_oos_trades.extend(oos.trades)
            plateau_parameters.extend(item[1] for item in plateau)
            windows.append(
                {
                    "train_start": train[0].event.detected_at.isoformat(),
                    "train_end": train[-1].event.detected_at.isoformat(),
                    "test_start": test[0].event.detected_at.isoformat(),
                    "test_end": test[-1].event.detected_at.isoformat(),
                    "best_train_objective": best_score,
                    "plateau_size": len(plateau),
                    "selected": dataclasses.asdict(selected),
                    "oos_metrics": dataclasses.asdict(oos.metrics),
                }
            )
            start += test_events
        return {
            "windows": windows,
            "oos_metrics": dataclasses.asdict(calculate_metrics(all_oos_trades)),
            "robust_ranges": self._robust_ranges(plateau_parameters),
            "selection_rule": (
                "Within 15% of the best train objective, choose the medoid-like parameter set; "
                "report only its next chronological window as OOS."
            ),
        }

    @staticmethod
    def _objective(metrics: BacktestMetrics) -> float:
        if metrics.trades == 0:
            return -1e12
        return (
            metrics.mean_pnl
            - metrics.max_drawdown / max(metrics.trades, 1)
            - 25 * metrics.forced_exit_rate
        )

    @staticmethod
    def _central_parameter(values: Sequence[SimulationParameters]) -> SimulationParameters:
        if len(values) == 1:
            return values[0]
        numeric_fields = (
            "stop_loss_fraction",
            "profit_target_fraction",
            "max_holding_bars",
            "entry_delay_bars",
        )
        medians: dict[str, float] = {}
        for field in numeric_fields:
            present = [
                getattr(value, field) for value in values if getattr(value, field) is not None
            ]
            medians[field] = float(statistics.median(present)) if present else 0.0

        def distance(value: SimulationParameters) -> float:
            total = 0.0
            for field in numeric_fields:
                raw = getattr(value, field)
                current = float(raw) if raw is not None else 0.0
                scale = max(abs(medians[field]), 1.0)
                total += ((current - medians[field]) / scale) ** 2
            return total

        return min(values, key=distance)

    @staticmethod
    def _robust_ranges(values: Sequence[SimulationParameters]) -> dict[str, Any]:
        if not values:
            return {}
        result: dict[str, Any] = {}
        for field in (
            "stop_loss_fraction",
            "profit_target_fraction",
            "max_holding_bars",
            "entry_delay_bars",
            "opening_range_bars",
        ):
            present = sorted(
                float(getattr(value, field))
                for value in values
                if getattr(value, field) is not None
            )
            if present:
                result[field] = {
                    "min": present[0],
                    "median": statistics.median(present),
                    "max": present[-1],
                    "support": len(present),
                }
        strategies = {value.strategy.value for value in values}
        result["strategies"] = sorted(strategies)
        return result
