from __future__ import annotations

import math

from .config import RiskConfig
from .models import BrokerSnapshot, ExecutionDecision, PositionPlan, RiskSnapshot


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def kill_switch_active(self, risk: RiskSnapshot) -> bool:
        total_day_pnl = risk.realized_pnl_today + min(risk.unrealized_pnl, 0)
        return total_day_pnl <= -(risk.account_equity * self.config.max_daily_loss_fraction)

    def plan_short(
        self,
        entry_price: float,
        stop_price: float,
        target_price: float | None,
        risk: RiskSnapshot,
        broker: BrokerSnapshot,
        execution: ExecutionDecision,
    ) -> PositionPlan:
        reasons: list[str] = []
        if entry_price <= 0:
            reasons.append("entry price must be positive")
        if stop_price <= entry_price:
            reasons.append("short stop must be above entry")
        if target_price is not None and target_price >= entry_price:
            reasons.append("short target must be below entry")
        if self.kill_switch_active(risk):
            reasons.append("daily loss kill switch is active")
        if risk.open_positions >= self.config.max_concurrent_positions:
            reasons.append("maximum concurrent positions reached")
        if (
            self.config.no_averaging_into_losers
            and risk.symbol_position_quantity > 0
            and risk.symbol_unrealized_pnl < 0
        ):
            reasons.append("adding to a losing short is prohibited")
        if not execution.executable:
            reasons.append("execution gate rejected the trade")
        if reasons:
            return PositionPlan(
                accepted=False,
                quantity=0,
                risk_budget=0,
                worst_case_loss=0,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                reasons=tuple(reasons),
            )

        equity = min(risk.account_equity, self.config.account_equity)
        risk_budget = min(
            equity * self.config.risk_per_trade_fraction,
            equity * self.config.max_symbol_loss_fraction,
        )
        adverse_gap_reserve = entry_price * self.config.halt_gap_reserve_fraction
        risk_per_share = stop_price - entry_price + adverse_gap_reserve
        if broker.locate_required:
            risk_per_share += broker.locate_cost_per_share
        risk_quantity = math.floor(risk_budget / risk_per_share) if risk_per_share > 0 else 0
        notional_quantity = math.floor(
            equity * self.config.max_position_notional_fraction / entry_price
        )
        gross_capacity = max(
            equity * self.config.max_gross_short_fraction - risk.gross_short_notional,
            0,
        )
        gross_quantity = math.floor(gross_capacity / entry_price)
        quantity = min(
            risk_quantity,
            notional_quantity,
            gross_quantity,
            execution.executable_quantity,
        )
        if quantity <= 0:
            reasons.append("risk, gross exposure, or execution constraints reduce size to zero")
        worst_case = quantity * risk_per_share
        return PositionPlan(
            accepted=quantity > 0,
            quantity=quantity,
            risk_budget=risk_budget,
            worst_case_loss=worst_case,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            reasons=tuple(reasons),
        )
