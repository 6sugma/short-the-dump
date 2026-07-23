from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .backtest import BacktestEngine, SimulationParameters, StressScenario
from .config import AppConfig
from .models import (
    BorrowPoint,
    BrokerSnapshot,
    EventCase,
    EventReplay,
    MarketBar,
    Observation,
    RiskSnapshot,
    StrategyKind,
)
from .service import ResearchService
from .store import EventStore

DEMO_SYMBOLS = ("AERO", "BLZE", "CRGO", "DRVN", "EVOL", "FUSE", "GLXY", "HYPE")


def _session_start(session_date: date) -> datetime:
    # UTC is intentional for a portable fixture; production adapters must supply canonical timestamps.
    return datetime.combine(session_date, time(14, 30), tzinfo=UTC)


def _price_path(
    start: float, end: float, count: int, amplitude: float, phase: float
) -> list[float]:
    values: list[float] = []
    for index in range(count):
        progress = index / max(count - 1, 1)
        baseline = start + (end - start) * progress
        wobble = math.sin(progress * math.pi * 4 + phase) * amplitude * (1 - progress * 0.4)
        values.append(max(baseline + wobble, 0.20))
    return values


def _bars_for_event(symbol: str, day0: date, index: int) -> tuple[MarketBar, ...]:
    randomizer = random.Random(1000 + index)
    sessions = (day0, day0 + timedelta(days=1), day0 + timedelta(days=2), day0 + timedelta(days=3))
    paths = (
        _price_path(4.8 + index * 0.08, 3.35 + index * 0.04, 60, 0.32, index),
        _price_path(3.20 + index * 0.04, 2.45 + index * 0.03, 60, 0.13, index + 0.7),
        _price_path(2.50 + index * 0.03, 2.22 + index * 0.02, 60, 0.10, index + 1.1),
        _price_path(2.20 + index * 0.02, 2.05 + index * 0.02, 60, 0.08, index + 1.5),
    )
    bars: list[MarketBar] = []
    cumulative_value = 0.0
    cumulative_volume = 0
    previous = paths[0][0]
    for session_index, (session_date, prices) in enumerate(zip(sessions, paths, strict=True)):
        cumulative_value = 0.0
        cumulative_volume = 0
        for minute, close in enumerate(prices):
            open_price = (
                previous if minute else close * (1 + (0.025 if session_index == 0 else 0.01))
            )
            high = max(open_price, close) * (1 + randomizer.uniform(0.006, 0.022))
            low = min(open_price, close) * (1 - randomizer.uniform(0.005, 0.018))
            base_volume = max(220_000 - minute * 2_600, 18_000)
            if index == 5:
                base_volume = max(12_000 - minute * 100, 1_800)
            volume = int(base_volume * (1 + randomizer.uniform(-0.20, 0.20)))
            typical = (high + low + close) / 3
            cumulative_value += typical * volume
            cumulative_volume += volume
            vwap = cumulative_value / cumulative_volume
            midpoint = close
            spread_fraction = 0.006 + (0.004 if index in {3, 5} else 0)
            halted = index == 6 and session_index == 0 and minute in {12, 13, 14}
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=_session_start(session_date) + timedelta(minutes=minute),
                    open=round(open_price, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close, 4),
                    volume=0 if halted else volume,
                    vwap=round(vwap, 4),
                    bid=round(midpoint * (1 - spread_fraction / 2), 4),
                    ask=round(midpoint * (1 + spread_fraction / 2), 4),
                    halted=halted,
                    session_index=session_index,
                )
            )
            previous = close
    return tuple(bars)


def build_demo_replays(events: list[EventCase]) -> list[EventReplay]:
    replays: list[EventReplay] = []
    for index, event in enumerate(events):
        bars = _bars_for_event(event.symbol, event.day0_date, index)
        initial = _session_start(event.day0_date) - timedelta(minutes=5)
        borrow = [
            BorrowPoint(
                timestamp=initial,
                available_shares=2_500 if index != 4 else 275,
                fee_annualized=0.55 + index * 0.08,
                locate_cost_per_share=0.025 + index * 0.004,
            )
        ]
        if index == 2:
            borrow.append(
                BorrowPoint(
                    timestamp=_session_start(event.day0_date + timedelta(days=1))
                    + timedelta(minutes=25),
                    available_shares=0,
                    fee_annualized=2.5,
                    recalled=True,
                    locate_cost_per_share=0.03,
                )
            )
        replays.append(
            EventReplay(
                event=event,
                bars=bars,
                borrow=tuple(borrow),
                signal_at=_session_start(event.day0_date) + timedelta(minutes=35),
                desired_quantity=500,
                rule201_active=index in {1, 4},
                spread_bps_fallback=80,
            )
        )
    return replays


def seed_demo(store: EventStore, config: AppConfig) -> dict[str, Any]:
    """Load clearly labeled deterministic fixtures and source-backed demo outputs."""
    service = ResearchService(config, store)
    events: list[EventCase] = []
    for index, symbol in enumerate(DEMO_SYMBOLS):
        day0 = date(2025, 1, 6) + timedelta(days=index * 7)
        observed_at = _session_start(day0) + timedelta(minutes=59, seconds=5)
        price = 3.35 + index * 0.04
        float_shares = 2_600_000 + index * 260_000
        cumulative_volume = 12_000_000 + index * 850_000
        snapshot_payload = {
            "exchange": "NASDAQ" if index % 2 == 0 else "NYSEAMERICAN",
            "price": price,
            "previous_close": 1.80 + index * 0.03,
            "session_high": 5.20 + index * 0.12,
            "vwap": 4.05 + index * 0.05,
            "cumulative_volume": cumulative_volume,
            "average_daily_volume": 850_000 + index * 30_000,
            "dollar_volume": cumulative_volume * price,
            "average_bar_dollar_volume": 420_000 if index != 5 else 42_000,
            "float_shares": float_shares,
            "shares_outstanding": float_shares * 2,
            "market_cap": price * float_shares * 2,
        }
        market_observation = Observation.create(
            symbol=symbol,
            kind="market_snapshot",
            effective_at=observed_at - timedelta(seconds=5),
            observed_at=observed_at,
            source="synthetic-fixture",
            source_ref=f"fixture://{symbol}/market/day0",
            payload=snapshot_payload,
        )
        service.ingest(market_observation)
        event, reasons = service.detect(market_observation)
        if event is None:
            raise RuntimeError(f"demo event did not qualify: {symbol}: {reasons}")
        events.append(event)
        bars = _bars_for_event(symbol, day0, index)
        for bar in (bar for bar in bars if bar.timestamp.date() == day0):
            service.ingest(
                Observation.create(
                    symbol=symbol,
                    event_id=event.id,
                    kind="bar",
                    effective_at=bar.timestamp,
                    observed_at=bar.timestamp + timedelta(seconds=2),
                    source="synthetic-fixture",
                    source_ref=f"fixture://{symbol}/bar/{bar.timestamp.isoformat()}",
                    payload={
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "vwap": bar.vwap,
                        "bid": bar.bid,
                        "ask": bar.ask,
                        "halted": bar.halted,
                        "session_index": 0,
                    },
                )
            )
        old_headline = "Company provides corporate update and explores strategic opportunities"
        current_headlines = (
            "Company provides corporate update and explores strategic opportunities",
            "Company announces non-binding memorandum for next generation platform",
            "Company receives purchase order with stated value",
            "Revolutionary company schedules investor webinar!",
        )
        service.ingest(
            Observation.create(
                symbol=symbol,
                event_id=event.id,
                kind="news",
                effective_at=observed_at - timedelta(days=45),
                observed_at=observed_at - timedelta(days=45) + timedelta(minutes=1),
                source="issuer",
                source_ref=f"fixture://{symbol}/news/old",
                payload={"headline": old_headline, "issuer_press_release": True},
            )
        )
        current_headline = current_headlines[index % len(current_headlines)]
        current_payload: dict[str, Any] = {
            "headline": current_headline,
            "summary": "Management described a large addressable market and a corporate milestone.",
            "issuer_press_release": True,
            "paid_promotion": index == 3,
        }
        if index == 2:
            current_payload["stated_value"] = 8_000_000
        service.ingest(
            Observation.create(
                symbol=symbol,
                event_id=event.id,
                kind="news",
                effective_at=observed_at - timedelta(hours=2),
                observed_at=observed_at - timedelta(hours=2) + timedelta(seconds=20),
                source="business wire",
                source_ref=f"fixture://{symbol}/news/current",
                payload=current_payload,
            )
        )
        filing_text = (
            "The company may offer shares under an at-the-market sales agreement. "
            "The resale prospectus covers common shares issuable upon exercise of warrants."
            if index != 2
            else "Current report describing a customer purchase order."
        )
        service.ingest(
            Observation.create(
                symbol=symbol,
                event_id=event.id,
                kind="filing",
                effective_at=observed_at - timedelta(days=10),
                observed_at=observed_at - timedelta(days=10) + timedelta(minutes=2),
                source="sec",
                source_ref=f"https://www.sec.gov/Archives/fixture/{symbol}/s3.htm",
                payload={
                    "form": "S-3" if index != 2 else "8-K",
                    "title": "SEC filing",
                    "text": filing_text,
                    "status": "active",
                },
            )
        )
        service.ingest(
            Observation.create(
                symbol=symbol,
                event_id=event.id,
                kind="capital_structure",
                effective_at=observed_at - timedelta(days=2),
                observed_at=observed_at - timedelta(days=1),
                source="synthetic-fixture",
                source_ref=f"fixture://{symbol}/capital",
                payload={
                    "authorized_shares": 200_000_000,
                    "shares_outstanding": float_shares * 2,
                    "float_shares": float_shares,
                    "registered_offering_remaining": 18_000_000,
                    "market_cap": price * float_shares * 2,
                    "last_reverse_split_date": (day0 - timedelta(days=180)).isoformat(),
                },
            )
        )
        service.ingest(
            Observation.create(
                symbol=symbol,
                event_id=event.id,
                kind="social",
                effective_at=observed_at - timedelta(minutes=1),
                observed_at=observed_at,
                source="synthetic-fixture",
                source_ref=f"fixture://{symbol}/social",
                payload={"mentions": 650 + index * 50, "baseline_mean": 40, "baseline_std": 70},
            )
        )
        if index == 6:
            service.ingest(
                Observation.create(
                    symbol=symbol,
                    event_id=event.id,
                    kind="halt",
                    effective_at=_session_start(day0) + timedelta(minutes=12),
                    observed_at=_session_start(day0) + timedelta(minutes=12, seconds=3),
                    source="synthetic-fixture",
                    source_ref=f"fixture://{symbol}/halt/1",
                    payload={
                        "reason": "LUDP",
                        "resumed_at": (_session_start(day0) + timedelta(minutes=15)).isoformat(),
                    },
                )
            )
        if store.latest_feature_payload(event.id) is None:
            broker = BrokerSnapshot.create(
                event_id=event.id,
                broker="fixture-broker-a",
                observed_at=observed_at,
                shortable=index != 7,
                available_shares=5_000 if index != 7 else 0,
                locate_required=True,
                locate_cost_per_share=0.04 + index * 0.003,
                borrow_fee_annualized=4.0 if index == 4 else 0.80 + index * 0.10,
                bid=price * 0.995,
                ask=price * 1.005,
                margin_requirement=2.0,
                buying_power=50_000,
                rule201_active=index in {1, 4},
                price_test_supported=True,
                hard_to_borrow=True,
                locate_expires_at=observed_at + timedelta(hours=8),
                notes=("Synthetic broker fixture; not a live locate.",),
            )
            service.analyze(
                event.id,
                observed_at,
                broker=broker,
                risk_snapshot=RiskSnapshot(
                    observed_at=observed_at,
                    account_equity=config.risk.account_equity,
                    realized_pnl_today=-50,
                    unrealized_pnl=0,
                    gross_short_notional=0,
                    open_positions=0,
                ),
                strategy=StrategyKind.DAY_PLUS_1,
            )

    if not store.latest_backtest_payloads(limit=1):
        engine = BacktestEngine(config.backtest)
        replays = build_demo_replays(events)
        base = SimulationParameters(max_holding_bars=90)
        for result in engine.compare_strategies(replays, base).values():
            store.save_backtest(result)
        stress = StressScenario(
            label="recall-and-buy-in-stress",
            slippage_multiplier=1.75,
            volume_haircut=0.35,
            borrow_fee_multiplier=1.5,
            force_buy_in_after_bars=45,
        )
        store.save_backtest(engine.run(replays, base, stress))
    return {
        "fixtures": True,
        "events": len(events),
        "candidates": len(store.list_decision_payloads()),
        "backtests": len(store.latest_backtest_payloads()),
        "warning": "All demo symbols and results are deterministic synthetic fixtures.",
    }
