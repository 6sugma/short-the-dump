from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from short_the_dump.api import create_app
from short_the_dump.config import AppConfig
from short_the_dump.demo import seed_demo
from short_the_dump.models import OperationalMode
from short_the_dump.store import EventStore
from short_the_dump.workflow import ApprovalWorkflow


def test_demo_seed_is_idempotent_and_source_labeled(tmp_path) -> None:
    config = AppConfig()
    with EventStore(tmp_path / "demo.db") as store:
        first = seed_demo(store, config)
        second = seed_demo(store, config)
        assert first["events"] == 8
        assert second["events"] == 8
        assert len(store.list_events(limit=100)) == 8
        assert all(event.source == "synthetic-fixture" for event in store.list_events(limit=100))
        assert store.verify_audit_chain()[0] is True


def test_alert_only_approval_records_review_without_order(tmp_path) -> None:
    config = AppConfig()
    with EventStore(tmp_path / "workflow.db") as store:
        seed_demo(store, config)
        decision = next(
            item
            for item in store.list_decision_payloads()
            if item.get("execution", {}).get("executable")
            and item.get("position_plan", {}).get("accepted")
        )
        response = ApprovalWorkflow(config, store).approve(
            decision["id"], "test operator", "reviewed point-in-time evidence"
        )
        assert response["order_created"] is False
        assert store.count_open_orders_for_event(decision["event_id"]) == 0


def test_paper_mode_creates_order_only_after_manual_approval(tmp_path) -> None:
    config = AppConfig()
    config = dataclasses.replace(
        config, runtime=dataclasses.replace(config.runtime, mode=OperationalMode.PAPER)
    ).validate()
    with EventStore(tmp_path / "paper.db") as store:
        seed_demo(store, config)
        decision = next(
            item
            for item in store.list_decision_payloads()
            if item.get("execution", {}).get("executable")
            and item.get("position_plan", {}).get("accepted")
        )
        response = ApprovalWorkflow(config, store).approve(
            decision["id"], "test operator", "paper trade approved for test"
        )
        assert response["order_created"] is True
        assert store.count_open_orders_for_event(decision["event_id"]) == 1
        assert store.get_decision_payload(decision["id"])["status"] == "ordered"


def test_api_dashboard_and_mutation_guard(tmp_path, monkeypatch) -> None:
    config = AppConfig()
    store = EventStore(tmp_path / "api.db")
    seed_demo(store, config)
    monkeypatch.delenv("SHORT_THE_DUMP_OPERATOR_TOKEN", raising=False)
    app = create_app(config, store)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        health = client.get("/api/health").json()
        assert health["mode"] == "alert_only"
        assert health["mutation_api_enabled"] is False
        candidates = client.get("/api/candidates").json()
        assert len(candidates) == 8
        detail = client.get(f"/api/candidates/{candidates[0]['id']}").json()
        assert "analogs" in detail and "broker" in detail
        blocked = client.post(
            f"/api/candidates/{candidates[0]['id']}/reject",
            json={"operator": "tester", "reason": "test rejection"},
        )
        assert blocked.status_code == 503
    store.close()
