from __future__ import annotations

import math
from datetime import datetime

from .config import ExecutionConfig
from .models import (
    BrokerSnapshot,
    ExecutionDecision,
    FeatureSnapshot,
    GateCheck,
    ensure_utc,
)


class ExecutionGate:
    """Broker-specific pre-trade feasibility gate. Every failed check is retained."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def evaluate(
        self,
        feature: FeatureSnapshot,
        broker: BrokerSnapshot,
        requested_quantity: int,
        now: datetime,
        *,
        average_bar_dollar_volume: float,
        currently_halted: bool = False,
    ) -> ExecutionDecision:
        now = ensure_utc(now)
        midpoint = broker.midpoint
        checks: list[GateCheck] = []

        def add(name: str, passed: bool, measured: object, limit: object, reason: str) -> None:
            checks.append(GateCheck(name, passed, measured, limit, reason))

        age = max((now - ensure_utc(broker.observed_at)).total_seconds(), 0)
        add(
            "inventory_freshness",
            age <= self.config.inventory_stale_after_seconds,
            age,
            self.config.inventory_stale_after_seconds,
            f"broker inventory is {age:.0f}s old",
        )
        add(
            "market_data_freshness",
            feature.data_freshness_seconds <= self.config.market_data_stale_after_seconds,
            feature.data_freshness_seconds,
            self.config.market_data_stale_after_seconds,
            f"market feature snapshot is {feature.data_freshness_seconds:.0f}s old",
        )
        add(
            "shortable",
            broker.shortable,
            broker.shortable,
            True,
            f"{broker.broker} does not mark the symbol shortable",
        )
        add(
            "minimum_inventory",
            broker.available_shares >= self.config.min_shortable_shares,
            broker.available_shares,
            self.config.min_shortable_shares,
            f"only {broker.available_shares:,} shares are available",
        )
        add(
            "borrow_fee",
            broker.borrow_fee_annualized <= self.config.max_borrow_fee_annualized,
            broker.borrow_fee_annualized,
            self.config.max_borrow_fee_annualized,
            f"annualized borrow fee {broker.borrow_fee_annualized:.1%} exceeds the limit",
        )
        locate_bps = (
            broker.locate_cost_per_share / midpoint * 10_000
            if broker.locate_required and midpoint > 0
            else 0.0
        )
        add(
            "locate_cost",
            locate_bps <= self.config.max_locate_cost_bps,
            locate_bps,
            self.config.max_locate_cost_bps,
            f"locate cost {locate_bps:.0f} bps exceeds the limit",
        )
        add(
            "spread",
            broker.spread_bps <= self.config.max_spread_bps,
            broker.spread_bps,
            self.config.max_spread_bps,
            f"quoted spread {broker.spread_bps:.0f} bps exceeds the limit",
        )
        add(
            "bar_liquidity",
            average_bar_dollar_volume >= self.config.min_average_bar_dollar_volume,
            average_bar_dollar_volume,
            self.config.min_average_bar_dollar_volume,
            f"average bar dollar volume ${average_bar_dollar_volume:,.0f} is too low",
        )
        add(
            "halt_state",
            not (currently_halted and self.config.reject_during_halt),
            currently_halted,
            False,
            "the symbol is currently halted",
        )
        price_test_ok = not broker.rule201_active or (
            broker.price_test_supported or not self.config.require_rule201_price_test_support
        )
        add(
            "rule201_price_test",
            price_test_ok,
            {"active": broker.rule201_active, "supported": broker.price_test_supported},
            {"support_required": self.config.require_rule201_price_test_support},
            "Rule 201 is active and the broker adapter cannot enforce the price test",
        )
        locate_valid = (
            broker.locate_expires_at is None or ensure_utc(broker.locate_expires_at) > now
        )
        add(
            "locate_expiry",
            locate_valid,
            broker.locate_expires_at.isoformat() if broker.locate_expires_at else None,
            now.isoformat(),
            "the locate has expired",
        )
        add(
            "quote_integrity",
            midpoint > 0 and broker.ask >= broker.bid > 0,
            {"bid": broker.bid, "ask": broker.ask},
            "positive non-crossed quote",
            "the broker quote is invalid or crossed",
        )

        liquidity_quantity = (
            math.floor(average_bar_dollar_volume / midpoint * self.config.max_volume_participation)
            if midpoint > 0
            else 0
        )
        margin_per_share = midpoint * max(broker.margin_requirement, 1.0)
        buying_power_quantity = (
            math.floor(broker.buying_power / margin_per_share) if margin_per_share > 0 else 0
        )
        executable_quantity = max(
            0,
            min(
                requested_quantity,
                broker.available_shares,
                liquidity_quantity,
                buying_power_quantity,
            ),
        )
        add(
            "executable_size",
            executable_quantity >= self.config.min_shortable_shares,
            executable_quantity,
            self.config.min_shortable_shares,
            f"realistic size is only {executable_quantity:,} shares after inventory, liquidity, and margin caps",
        )
        estimated_margin = executable_quantity * midpoint * max(broker.margin_requirement, 1.0)
        add(
            "buying_power",
            estimated_margin <= broker.buying_power,
            estimated_margin,
            broker.buying_power,
            "initial margin exceeds broker buying power",
        )

        executable = requested_quantity > 0 and all(check.passed for check in checks)
        return ExecutionDecision(
            executable=executable,
            requested_quantity=requested_quantity,
            executable_quantity=executable_quantity if executable else 0,
            estimated_locate_cost=(
                executable_quantity * broker.locate_cost_per_share if executable else 0.0
            ),
            estimated_initial_margin=estimated_margin if executable else 0.0,
            checks=tuple(checks),
        )


def expected_borrow_cost(
    notional: float, annualized_fee: float, holding_hours: float, day_count: int = 360
) -> float:
    return max(notional, 0) * max(annualized_fee, 0) * max(holding_hours, 0) / (day_count * 24)
