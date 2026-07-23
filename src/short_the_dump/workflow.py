from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .config import AppConfig
from .models import Approval, EventStatus, OperationalMode, new_id
from .store import EventStore


class OrderRouter(Protocol):
    name: str

    def submit_short(self, order: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class PaperOrderRouter:
    name: str = "internal-paper"

    def submit_short(self, order: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "router": self.name,
            "status": "accepted",
            "external_order_id": f"paper_{new_id('ext')}",
            "order": dict(order),
        }


class ApprovalWorkflow:
    def __init__(
        self,
        config: AppConfig,
        store: EventStore,
        paper_router: OrderRouter | None = None,
        live_router: OrderRouter | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.paper_router = paper_router or PaperOrderRouter()
        self.live_router = live_router

    def approve(self, decision_id: str, operator: str, reason: str) -> dict[str, Any]:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and approval reason are required")
        decision = self.store.get_decision_payload(decision_id)
        if not decision:
            raise KeyError(f"decision not found: {decision_id}")
        execution = decision.get("execution")
        plan = decision.get("position_plan")
        if not execution or not execution.get("executable"):
            raise ValueError("non-executable candidates cannot be approved")
        if not plan or not plan.get("accepted") or int(plan.get("quantity", 0)) <= 0:
            raise ValueError("candidate does not have an accepted positive-size risk plan")
        if (
            self.config.runtime.mode is not OperationalMode.ALERT_ONLY
            and self.store.count_open_orders_for_event(str(decision["event_id"]))
        ):
            raise ValueError("an order already exists for this event; pyramiding is disabled")
        self.store.record_approval(
            Approval(decision_id=decision_id, operator=operator, action="approve", reason=reason)
        )
        if self.config.runtime.mode is OperationalMode.ALERT_ONLY:
            return {
                "decision_id": decision_id,
                "status": EventStatus.APPROVED.value,
                "order_created": False,
                "message": "Research approval recorded; alert-only mode prohibits order creation.",
            }
        return self._submit(decision, operator)

    def reject(self, decision_id: str, operator: str, reason: str) -> dict[str, Any]:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and rejection reason are required")
        self.store.record_approval(
            Approval(decision_id=decision_id, operator=operator, action="reject", reason=reason)
        )
        return {"decision_id": decision_id, "status": EventStatus.REJECTED.value}

    def _submit(self, decision: Mapping[str, Any], operator: str) -> dict[str, Any]:
        event_id = str(decision["event_id"])
        if self.store.count_open_orders_for_event(event_id):
            raise ValueError("an order already exists for this event; pyramiding is disabled")
        plan = decision["position_plan"]
        order = {
            "client_order_id": new_id("ord"),
            "decision_id": decision["id"],
            "event_id": event_id,
            "side": "sell_short",
            "quantity": int(plan["quantity"]),
            "limit_price": float(plan["entry_price"]),
            "stop_price": float(plan["stop_price"]),
            "target_price": plan.get("target_price"),
            "manual_approver": operator,
        }
        if self.config.runtime.mode is OperationalMode.PAPER:
            router = self.paper_router
        elif self.config.runtime.mode is OperationalMode.LIMITED_LIVE:
            if not self.config.runtime.live_order_routing_enabled or self.live_router is None:
                raise RuntimeError("live routing interlock or live router is unavailable")
            router = self.live_router
        else:
            raise RuntimeError("alert-only mode cannot submit orders")
        response = dict(router.submit_short(order))
        status = str(response.get("status", "rejected"))
        self.store.save_order(
            order_id=order["client_order_id"],
            decision_id=str(decision["id"]),
            event_id=event_id,
            mode=self.config.runtime.mode.value,
            status=status,
            payload={"request": order, "response": response},
            actor=operator,
        )
        target_status = EventStatus.ORDERED if status == "accepted" else EventStatus.REJECTED
        self.store.update_decision_status(
            str(decision["id"]),
            target_status,
            operator,
            f"router {router.name} returned {status}",
        )
        return {
            "decision_id": decision["id"],
            "status": target_status.value,
            "order_created": status == "accepted",
            "order": order,
            "router_response": response,
        }
