from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import AppConfig, load_config
from .models import EventStatus
from .service import ResearchService
from .store import EventStore
from .workflow import ApprovalWorkflow


class ReviewRequest(BaseModel):
    operator: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=5, max_length=500)


def create_app(
    config: AppConfig | None = None,
    store: EventStore | None = None,
) -> FastAPI:
    app_config = (config or load_config()).validate()
    owns_store = store is None
    event_store = store or EventStore(app_config.runtime.database_path)
    workflow = ApprovalWorkflow(app_config, event_store)
    research = ResearchService(app_config, event_store)
    web_dir = Path(__file__).with_name("web")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owns_store:
            event_store.close()

    app = FastAPI(
        title="Short the Dump",
        version="0.1.0",
        description="Alert-first point-in-time reversal research platform.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.config = app_config
    app.state.store = event_store
    app.state.research = research
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_operator_token(
        x_operator_token: Annotated[str | None, Header()] = None,
    ) -> None:
        configured = os.getenv("SHORT_THE_DUMP_OPERATOR_TOKEN")
        if not configured:
            raise HTTPException(
                status_code=503,
                detail="Dashboard mutations are disabled until SHORT_THE_DUMP_OPERATOR_TOKEN is set.",
            )
        if x_operator_token != configured:
            raise HTTPException(status_code=401, detail="invalid operator token")

    @app.exception_handler(KeyError)
    async def key_error_handler(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        audit_ok, bad_sequence = event_store.verify_audit_chain()
        return {
            "status": "ok" if audit_ok else "degraded",
            "mode": app_config.runtime.mode.value,
            "manual_approval_required": app_config.runtime.manual_approval_required,
            "live_order_routing_enabled": app_config.runtime.live_order_routing_enabled,
            "audit_chain_valid": audit_ok,
            "audit_bad_sequence": bad_sequence,
            "mutation_api_enabled": bool(os.getenv("SHORT_THE_DUMP_OPERATOR_TOKEN")),
        }

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        return event_store.dashboard_summary() | {
            "mode": app_config.runtime.mode.value,
            "fixture_data": any(
                event.source == "synthetic-fixture" for event in event_store.list_events(limit=1000)
            ),
            "risk_limits": {
                "risk_per_trade_fraction": app_config.risk.risk_per_trade_fraction,
                "max_daily_loss_fraction": app_config.risk.max_daily_loss_fraction,
                "max_concurrent_positions": app_config.risk.max_concurrent_positions,
                "max_gross_short_fraction": app_config.risk.max_gross_short_fraction,
                "no_averaging_into_losers": app_config.risk.no_averaging_into_losers,
            },
        }

    @app.get("/api/candidates")
    def candidates(
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        parsed_status: EventStatus | None = None
        if status:
            try:
                parsed_status = EventStatus(status)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid event status") from exc
        return event_store.list_decision_payloads(parsed_status, limit)

    @app.get("/api/candidates/{decision_id}")
    def candidate(decision_id: str) -> dict[str, Any]:
        payload = event_store.get_decision_payload(decision_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        event_id = str(payload["event_id"])
        event = event_store.get_event(event_id)
        return {
            "decision": payload,
            "event": None
            if event is None
            else {
                "id": event.id,
                "symbol": event.symbol,
                "exchange": event.exchange,
                "day0_date": event.day0_date.isoformat(),
                "detected_at": event.detected_at.isoformat(),
                "status": event.status.value,
                "baseline_close": event.baseline_close,
                "detection_price": event.detection_price,
                "source": event.source,
            },
            "feature": event_store.latest_feature_payload(event_id),
            "broker": event_store.latest_broker_payload(event_id),
            "analogs": research.analogs(event_id),
        }

    @app.get("/api/backtests")
    def backtests(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
        return event_store.latest_backtest_payloads(limit)

    @app.get("/api/audit")
    def audit(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return event_store.audit_rows(limit)

    @app.post(
        "/api/candidates/{decision_id}/approve", dependencies=[Depends(require_operator_token)]
    )
    def approve(decision_id: str, request: ReviewRequest) -> dict[str, Any]:
        return workflow.approve(decision_id, request.operator, request.reason)

    @app.post(
        "/api/candidates/{decision_id}/reject", dependencies=[Depends(require_operator_token)]
    )
    def reject(decision_id: str, request: ReviewRequest) -> dict[str, Any]:
        return workflow.reject(decision_id, request.operator, request.reason)

    return app
