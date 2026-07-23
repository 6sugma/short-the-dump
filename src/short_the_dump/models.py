from __future__ import annotations

import dataclasses
import enum
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any


class StrEnum(enum.StrEnum):
    pass


class OperationalMode(StrEnum):
    ALERT_ONLY = "alert_only"
    PAPER = "paper"
    LIMITED_LIVE = "limited_live"


class EventStatus(StrEnum):
    WATCH = "watch"
    ALERTED = "alerted"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ORDERED = "ordered"
    CLOSED = "closed"


class StrategyKind(StrEnum):
    SAME_DAY = "same_day"
    DAY_PLUS_1 = "day_plus_1"
    FIRST_RED_DAY = "first_red_day"
    DAY_PLUS_2 = "day_plus_2"


class OrderSide(StrEnum):
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"


class OrderStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ExitReason(StrEnum):
    TARGET = "target"
    STOP = "stop"
    MAX_HOLD = "max_hold"
    RECALL = "recall"
    FORCED_BUY_IN = "forced_buy_in"
    DATA_END = "data_end"
    RISK_KILL = "risk_kill"


class CatalystLabel(StrEnum):
    MATERIAL_NOVEL = "material_novel"
    MATERIAL_REHASH = "material_rehash"
    LOW_MATERIALITY = "low_materiality"
    PROMOTIONAL = "promotional"
    NONE = "none"


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_utc(parsed)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class EventCase:
    id: str
    symbol: str
    exchange: str
    day0_date: date
    detected_at: datetime
    status: EventStatus = EventStatus.WATCH
    baseline_close: float = 0.0
    detection_price: float = 0.0
    source: str = "unknown"

    @classmethod
    def create(
        cls,
        symbol: str,
        exchange: str,
        day0_date: date,
        detected_at: datetime,
        baseline_close: float,
        detection_price: float,
        source: str,
    ) -> EventCase:
        return cls(
            id=new_id("evt"),
            symbol=symbol.upper(),
            exchange=exchange.upper(),
            day0_date=day0_date,
            detected_at=ensure_utc(detected_at),
            baseline_close=baseline_close,
            detection_price=detection_price,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    symbol: str
    kind: str
    effective_at: datetime
    observed_at: datetime
    source: str
    payload: Mapping[str, Any]
    event_id: str | None = None
    source_ref: str | None = None

    @classmethod
    def create(
        cls,
        symbol: str,
        kind: str,
        effective_at: datetime,
        observed_at: datetime,
        source: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
        source_ref: str | None = None,
    ) -> Observation:
        return cls(
            id=new_id("obs"),
            symbol=symbol.upper(),
            kind=kind,
            effective_at=ensure_utc(effective_at),
            observed_at=ensure_utc(observed_at),
            source=source,
            payload=payload,
            event_id=event_id,
            source_ref=source_ref,
        )


@dataclass(frozen=True, slots=True)
class MarketBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
    bid: float | None = None
    ask: float | None = None
    halted: bool = False
    session_index: int = 0

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is inconsistent")
        if self.volume < 0:
            raise ValueError("bar volume must be non-negative")

    @property
    def midpoint(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return (self.high + self.low) / 2

    @property
    def spread_bps(self) -> float:
        if self.bid is None or self.ask is None or self.midpoint <= 0:
            return 0.0
        return (self.ask - self.bid) / self.midpoint * 10_000


@dataclass(frozen=True, slots=True)
class CatalystAssessment:
    label: CatalystLabel
    novelty: float
    credibility: float
    materiality: float
    promotional_risk: float
    confidence: float
    reasons: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SupplyRiskAssessment:
    score: float
    shelf_or_resale_active: bool
    atm_active: bool
    warrants_or_convertibles: bool
    reverse_split_recent: bool
    authorized_overhang_ratio: float | None
    reasons: tuple[str, ...] = ()
    filing_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    id: str
    event_id: str
    as_of: datetime
    price: float
    day0_return: float
    relative_volume: float
    dollar_volume: float
    float_shares: int | None
    float_turnover: float | None
    market_cap: float | None
    vwap_distance: float | None
    close_off_high: float
    lower_high_ratio: float
    failed_vwap_reclaims: int
    opening_range_breakdown: bool
    volume_fade_ratio: float | None
    halt_count: int
    social_attention_zscore: float | None
    catalyst: CatalystAssessment
    supply_risk: SupplyRiskAssessment
    momentum_failure_score: float
    data_freshness_seconds: float
    evidence: tuple[str, ...] = ()

    @classmethod
    def create(cls, **values: Any) -> FeatureSnapshot:
        return cls(id=new_id("feat"), **values)


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    id: str
    event_id: str
    broker: str
    observed_at: datetime
    shortable: bool
    available_shares: int
    locate_required: bool
    locate_cost_per_share: float
    borrow_fee_annualized: float
    bid: float
    ask: float
    margin_requirement: float
    buying_power: float
    rule201_active: bool
    price_test_supported: bool
    hard_to_borrow: bool = False
    locate_expires_at: datetime | None = None
    notes: tuple[str, ...] = ()

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.midpoint * 10_000 if self.midpoint else float("inf")

    @classmethod
    def create(cls, **values: Any) -> BrokerSnapshot:
        return cls(id=new_id("brk"), **values)


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    passed: bool
    measured: Any
    limit: Any
    reason: str


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    executable: bool
    requested_quantity: int
    executable_quantity: int
    estimated_locate_cost: float
    estimated_initial_margin: float
    checks: tuple[GateCheck, ...]

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        return tuple(check.reason for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    observed_at: datetime
    account_equity: float
    realized_pnl_today: float
    unrealized_pnl: float
    gross_short_notional: float
    open_positions: int
    symbol_position_quantity: int = 0
    symbol_average_price: float | None = None
    symbol_unrealized_pnl: float = 0.0


@dataclass(frozen=True, slots=True)
class PositionPlan:
    accepted: bool
    quantity: int
    risk_budget: float
    worst_case_loss: float
    entry_price: float
    stop_price: float
    target_price: float | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    id: str
    event_id: str
    feature_snapshot_id: str
    created_at: datetime
    strategy: StrategyKind
    score: float
    band: str
    status: EventStatus
    thesis: str
    evidence: tuple[str, ...]
    risk_flags: tuple[str, ...]
    execution: ExecutionDecision | None = None
    position_plan: PositionPlan | None = None

    @classmethod
    def create(cls, **values: Any) -> CandidateDecision:
        return cls(id=new_id("dec"), **values)


@dataclass(frozen=True, slots=True)
class Approval:
    decision_id: str
    operator: str
    action: str
    reason: str
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class SimOrder:
    id: str
    event_id: str
    strategy: StrategyKind
    side: OrderSide
    submitted_at: datetime
    quantity: int
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.ACCEPTED

    @classmethod
    def create(cls, **values: Any) -> SimOrder:
        return cls(id=new_id("ord"), **values)


@dataclass(frozen=True, slots=True)
class SimFill:
    order_id: str
    timestamp: datetime
    quantity: int
    price: float
    slippage_bps: float
    fees: float = 0.0


@dataclass(frozen=True, slots=True)
class BorrowPoint:
    timestamp: datetime
    available_shares: int
    fee_annualized: float
    recalled: bool = False
    locate_cost_per_share: float = 0.0


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    event_id: str
    symbol: str
    strategy: StrategyKind
    entry_at: datetime
    exit_at: datetime
    quantity: int
    average_entry: float
    average_exit: float
    gross_pnl: float
    net_pnl: float
    return_on_notional: float
    locate_cost: float
    borrow_cost: float
    slippage_cost: float
    exit_reason: ExitReason
    partial_fill: bool
    forced_action: bool


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    mean_pnl: float
    median_pnl: float
    mean_return: float
    max_drawdown: float
    profit_factor: float | None
    forced_exit_rate: float
    partial_fill_rate: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    id: str
    created_at: datetime
    strategy: StrategyKind
    parameters: Mapping[str, Any]
    metrics: BacktestMetrics
    trades: tuple[BacktestTrade, ...]
    stress_label: str = "base"

    @classmethod
    def create(cls, **values: Any) -> BacktestResult:
        return cls(id=new_id("bt"), created_at=utc_now(), **values)


@dataclass(frozen=True, slots=True)
class EventReplay:
    event: EventCase
    bars: tuple[MarketBar, ...]
    borrow: tuple[BorrowPoint, ...]
    signal_at: datetime
    desired_quantity: int
    rule201_active: bool = False
    spread_bps_fallback: float = 80.0


def weighted_average(fills: Sequence[SimFill]) -> float:
    quantity = sum(fill.quantity for fill in fills)
    if quantity <= 0:
        raise ValueError("cannot average empty fills")
    return sum(fill.price * fill.quantity for fill in fills) / quantity
