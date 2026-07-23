from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from short_the_dump.models import (
    BorrowPoint,
    BrokerSnapshot,
    EventCase,
    EventReplay,
    MarketBar,
)


@pytest.fixture
def event() -> EventCase:
    return EventCase.create(
        symbol="TEST",
        exchange="NASDAQ",
        day0_date=date(2025, 1, 6),
        detected_at=datetime(2025, 1, 6, 15, tzinfo=UTC),
        baseline_close=2.0,
        detection_price=4.0,
        source="test",
    )


@pytest.fixture
def broker(event: EventCase) -> BrokerSnapshot:
    observed = datetime(2025, 1, 6, 15, tzinfo=UTC)
    return BrokerSnapshot.create(
        event_id=event.id,
        broker="test-broker",
        observed_at=observed,
        shortable=True,
        available_shares=10_000,
        locate_required=True,
        locate_cost_per_share=0.03,
        borrow_fee_annualized=0.80,
        bid=3.98,
        ask=4.02,
        margin_requirement=1.5,
        buying_power=100_000,
        rule201_active=False,
        price_test_supported=True,
        hard_to_borrow=True,
        locate_expires_at=observed + timedelta(hours=8),
    )


def make_replay(
    event: EventCase,
    *,
    recalled: bool = False,
    volume: int = 10_000,
    halted_entry: bool = False,
) -> EventReplay:
    bars: list[MarketBar] = []
    day0 = event.day0_date
    previous = 4.0
    for session in range(3):
        session_date = day0 + timedelta(days=session)
        for minute in range(25):
            timestamp = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC) + timedelta(
                hours=14, minutes=30 + minute
            )
            trend = 4.0 - session * 0.45 - minute * 0.018
            open_price = previous
            close = max(trend, 1.0)
            halted = halted_entry and session == 0 and minute < 3
            bars.append(
                MarketBar(
                    symbol=event.symbol,
                    timestamp=timestamp,
                    open=open_price,
                    high=max(open_price, close) * 1.01,
                    low=min(open_price, close) * 0.99,
                    close=close,
                    volume=0 if halted else volume,
                    vwap=(open_price + close) / 2,
                    bid=close * 0.9975,
                    ask=close * 1.0025,
                    halted=halted,
                    session_index=session,
                )
            )
            previous = close
    borrow = [
        BorrowPoint(
            timestamp=bars[0].timestamp - timedelta(minutes=1),
            available_shares=1_000,
            fee_annualized=0.72,
            locate_cost_per_share=0.02,
        )
    ]
    if recalled:
        borrow.append(
            BorrowPoint(
                timestamp=bars[31].timestamp,
                available_shares=0,
                fee_annualized=2.0,
                recalled=True,
                locate_cost_per_share=0.02,
            )
        )
    return EventReplay(
        event=event,
        bars=tuple(bars),
        borrow=tuple(borrow),
        signal_at=bars[2].timestamp,
        desired_quantity=500,
    )
