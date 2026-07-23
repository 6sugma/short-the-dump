from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from .models import OperationalMode


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: OperationalMode = OperationalMode.ALERT_ONLY
    database_path: str = "short_the_dump.db"
    manual_approval_required: bool = True
    live_order_routing_enabled: bool = False
    data_stale_after_seconds: int = 90


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    allowed_exchanges: tuple[str, ...] = ("NASDAQ", "NYSE", "NYSEAMERICAN")
    min_price: float = 0.25
    max_price: float = 20.0
    max_float_shares: int = 20_000_000
    max_market_cap: float = 500_000_000
    min_day0_return: float = 0.50
    min_relative_volume: float = 5.0
    min_dollar_volume: float = 1_000_000


@dataclass(frozen=True, slots=True)
class SignalConfig:
    alert_score: float = 62.0
    high_conviction_score: float = 78.0
    minimum_catalyst_confidence: float = 0.20
    opening_range_minutes: int = 15
    lower_high_lookback_bars: int = 5
    failed_reclaim_tolerance_bps: float = 15.0


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    max_borrow_fee_annualized: float = 3.0
    max_locate_cost_bps: float = 500.0
    max_spread_bps: float = 150.0
    min_shortable_shares: int = 100
    max_volume_participation: float = 0.05
    min_average_bar_dollar_volume: float = 25_000
    require_rule201_price_test_support: bool = True
    reject_during_halt: bool = True
    inventory_stale_after_seconds: int = 60
    market_data_stale_after_seconds: int = 90


@dataclass(frozen=True, slots=True)
class RiskConfig:
    account_equity: float = 100_000.0
    risk_per_trade_fraction: float = 0.0025
    max_position_notional_fraction: float = 0.05
    max_gross_short_fraction: float = 0.15
    max_daily_loss_fraction: float = 0.01
    max_concurrent_positions: int = 3
    max_symbol_loss_fraction: float = 0.004
    halt_gap_reserve_fraction: float = 0.30
    no_averaging_into_losers: bool = True


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    base_slippage_bps: float = 20.0
    spread_cross_fraction: float = 0.5
    impact_bps_at_max_participation: float = 80.0
    max_volume_participation: float = 0.05
    forced_buy_in_slippage_bps: float = 250.0
    recall_cover_delay_bars: int = 1
    locate_expiry_hours: int = 24


@dataclass(frozen=True, slots=True)
class AppConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    def validate(self) -> AppConfig:
        if self.runtime.mode is OperationalMode.LIMITED_LIVE:
            if not self.runtime.live_order_routing_enabled:
                raise ValueError("limited_live mode requires live_order_routing_enabled=true")
            acknowledgement = os.getenv("SHORT_THE_DUMP_LIVE_ACK", "")
            if acknowledgement != "I_UNDERSTAND_LIVE_SHORT_RISK":
                raise ValueError(
                    "limited_live mode requires the deployment-time live-risk interlock"
                )
        elif self.runtime.live_order_routing_enabled:
            raise ValueError("live routing cannot be enabled outside limited_live mode")
        if not self.runtime.manual_approval_required:
            raise ValueError("manual approval is mandatory in this release")
        if not 0 < self.risk.risk_per_trade_fraction <= self.risk.max_daily_loss_fraction:
            raise ValueError("risk_per_trade_fraction must be positive and within daily loss limit")
        if not 0 < self.execution.max_volume_participation <= 0.20:
            raise ValueError("execution participation must be in (0, 0.20]")
        if not 0 < self.backtest.max_volume_participation <= 0.20:
            raise ValueError("backtest participation must be in (0, 0.20]")
        if self.risk.no_averaging_into_losers is not True:
            raise ValueError("no_averaging_into_losers is a mandatory safety invariant")
        return self


T = TypeVar("T")


def _construct(cls: type[T], raw: Mapping[str, Any] | None) -> T:
    values = dict(raw or {})
    field_names = {item.name for item in dataclasses.fields(cls)}
    unknown = sorted(set(values) - field_names)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} settings: {', '.join(unknown)}")
    if cls is RuntimeConfig and "mode" in values:
        values["mode"] = OperationalMode(values["mode"])
    if cls is DetectionConfig and "allowed_exchanges" in values:
        values["allowed_exchanges"] = tuple(str(v).upper() for v in values["allowed_exchanges"])
    return cls(**values)


def load_config(path: str | Path | None = None) -> AppConfig:
    raw: Mapping[str, Any] = {}
    if path is not None:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
    known = {"runtime", "detection", "signals", "execution", "risk", "backtest"}
    unknown_sections = sorted(set(raw) - known)
    if unknown_sections:
        raise ValueError(f"unknown configuration sections: {', '.join(unknown_sections)}")
    config = AppConfig(
        runtime=_construct(RuntimeConfig, raw.get("runtime")),
        detection=_construct(DetectionConfig, raw.get("detection")),
        signals=_construct(SignalConfig, raw.get("signals")),
        execution=_construct(ExecutionConfig, raw.get("execution")),
        risk=_construct(RiskConfig, raw.get("risk")),
        backtest=_construct(BacktestConfig, raw.get("backtest")),
    )
    return config.validate()
