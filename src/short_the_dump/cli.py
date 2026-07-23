from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .backtest import BacktestEngine, SimulationParameters, StressScenario, WalkForwardOptimizer
from .config import AppConfig, load_config
from .connectors.replay import JsonlObservationProvider
from .demo import build_demo_replays, seed_demo
from .models import EventStatus, StrategyKind, jsonable
from .service import ResearchService
from .store import EventStore
from .workflow import ApprovalWorkflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="short-the-dump",
        description="Point-in-time small-cap reversal research and paper-trading platform.",
    )
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("--db", type=Path, help="override SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="initialize the event database")
    sub.add_parser("seed-demo", help="load deterministic synthetic fixtures")

    serve = sub.add_parser("serve", help="run the local API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--seed-demo", action="store_true")

    ingest = sub.add_parser("ingest-jsonl", help="append normalized point-in-time observations")
    ingest.add_argument("path", type=Path)
    ingest.add_argument(
        "--detect", action="store_true", help="run extreme-move detection on snapshots"
    )

    candidates = sub.add_parser("candidates", help="list candidate decisions")
    candidates.add_argument("--status", choices=[item.value for item in EventStatus])
    candidates.add_argument("--limit", type=int, default=100)

    for action in ("approve", "reject"):
        review = sub.add_parser(action, help=f"manually {action} a candidate")
        review.add_argument("decision_id")
        review.add_argument("--operator", required=True)
        review.add_argument("--reason", required=True)

    backtest = sub.add_parser("backtest-demo", help="compare the four entry timings on fixtures")
    backtest.add_argument("--stress", action="store_true")
    backtest.add_argument("--max-holding-bars", type=int, default=90)

    walk = sub.add_parser("walk-forward-demo", help="run chronological parameter plateau selection")
    walk.add_argument("--train-events", type=int, default=4)
    walk.add_argument("--test-events", type=int, default=2)

    sub.add_parser("verify-audit", help="verify the tamper-evident audit chain")
    return parser


def _config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    if args.db:
        config = dataclasses.replace(
            config,
            runtime=dataclasses.replace(config.runtime, database_path=str(args.db)),
        )
    return config.validate()


def _print(value: Any) -> None:
    print(json.dumps(jsonable(value), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = _config(args)
    if args.command == "serve":
        if args.seed_demo:
            with EventStore(config.runtime.database_path) as store:
                seed_demo(store, config)
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")
        return 0

    with EventStore(config.runtime.database_path) as store:
        if args.command == "init-db":
            _print({"database": config.runtime.database_path, "schema_version": 1})
        elif args.command == "seed-demo":
            _print(seed_demo(store, config))
        elif args.command == "ingest-jsonl":
            service = ResearchService(config, store)
            appended = 0
            detected: list[str] = []
            rejected: list[dict[str, Any]] = []
            for observation in JsonlObservationProvider(args.path).observations():
                appended += int(service.ingest(observation))
                if args.detect and observation.kind == "market_snapshot":
                    event, reasons = service.detect(observation)
                    if event:
                        detected.append(event.id)
                    else:
                        rejected.append({"symbol": observation.symbol, "reasons": reasons})
            _print({"appended": appended, "detected_event_ids": detected, "rejected": rejected})
        elif args.command == "candidates":
            status = EventStatus(args.status) if args.status else None
            _print(store.list_decision_payloads(status, args.limit))
        elif args.command in {"approve", "reject"}:
            workflow = ApprovalWorkflow(config, store)
            method = workflow.approve if args.command == "approve" else workflow.reject
            _print(method(args.decision_id, args.operator, args.reason))
        elif args.command == "backtest-demo":
            seed_demo(store, config)
            events = sorted(store.list_events(limit=100), key=lambda item: item.detected_at)
            replays = build_demo_replays(events)
            engine = BacktestEngine(config.backtest)
            stress = (
                StressScenario(
                    label="cli-stress",
                    slippage_multiplier=1.75,
                    volume_haircut=0.35,
                    borrow_fee_multiplier=1.5,
                    force_buy_in_after_bars=45,
                )
                if args.stress
                else None
            )
            base = SimulationParameters(max_holding_bars=args.max_holding_bars)
            results = engine.compare_strategies(replays, base, stress)
            for result in results.values():
                store.save_backtest(result)
            _print({key.value: value.metrics for key, value in results.items()})
        elif args.command == "walk-forward-demo":
            seed_demo(store, config)
            events = sorted(store.list_events(limit=100), key=lambda item: item.detected_at)
            replays = build_demo_replays(events)
            engine = BacktestEngine(config.backtest)
            grid = [
                SimulationParameters(
                    strategy=strategy,
                    stop_loss_fraction=stop,
                    profit_target_fraction=target,
                    max_holding_bars=hold,
                )
                for strategy in StrategyKind
                for stop in (0.18, 0.25, 0.35)
                for target in (0.20, 0.30, 0.40)
                for hold in (45, 90, 180)
            ]
            result = WalkForwardOptimizer(engine).evaluate(
                replays,
                grid,
                train_events=args.train_events,
                test_events=args.test_events,
            )
            _print(result)
        elif args.command == "verify-audit":
            valid, sequence = store.verify_audit_chain()
            _print({"valid": valid, "first_bad_sequence": sequence})
            return 0 if valid else 2
        else:
            raise AssertionError(f"unhandled command: {args.command}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
