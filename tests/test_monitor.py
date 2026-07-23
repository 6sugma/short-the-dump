from __future__ import annotations

from datetime import UTC, datetime, timedelta

from short_the_dump.config import AppConfig
from short_the_dump.models import BrokerSnapshot, Observation, RiskSnapshot
from short_the_dump.monitor import ConnectorBundle, RealtimeMonitor
from short_the_dump.service import ResearchService
from short_the_dump.store import EventStore


class FakeMarket:
    name = "fake-market"

    def __init__(self, price: float = 3.5) -> None:
        self.price = price

    def market_snapshot(self, symbol: str, as_of: datetime) -> Observation:
        return Observation.create(
            symbol,
            "market_snapshot",
            as_of,
            as_of,
            self.name,
            {
                "exchange": "NASDAQ",
                "price": self.price,
                "previous_close": 1.5,
                "session_high": 5.0,
                "vwap": 4.0,
                "cumulative_volume": 10_000_000,
                "average_daily_volume": 500_000,
                "float_shares": 2_000_000,
                "market_cap": 30_000_000,
                "average_bar_dollar_volume": 250_000,
            },
            source_ref=f"fake://{symbol}/snapshot/{as_of.isoformat()}",
        )

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Observation]:
        result = []
        for index in range(20):
            timestamp = end - timedelta(minutes=19 - index)
            high = 4.5 - index * 0.07
            close = high - 0.15
            result.append(
                Observation.create(
                    symbol,
                    "bar",
                    timestamp,
                    timestamp,
                    self.name,
                    {
                        "open": high - 0.05,
                        "high": high,
                        "low": close - 0.05,
                        "close": close,
                        "volume": 100_000 - index * 2_000,
                        "vwap": 4.0,
                    },
                    source_ref=f"fake://{symbol}/bar/{timestamp.isoformat()}",
                )
            )
        return result

    def halts(self, symbol: str, start: datetime, end: datetime) -> list[Observation]:
        return []


class FakeNews:
    name = "fake-news"

    def news(self, symbol: str, start: datetime, end: datetime) -> list[Observation]:
        return [
            Observation.create(
                symbol,
                "news",
                end - timedelta(hours=1),
                end - timedelta(hours=1),
                self.name,
                {"headline": "Company schedules investor webinar", "issuer_press_release": True},
                source_ref=f"fake://{symbol}/news/1",
            )
        ]


class FakeSocial:
    name = "fake-social"

    def attention(self, symbol: str, as_of: datetime) -> Observation:
        return Observation.create(
            symbol,
            "social",
            as_of,
            as_of,
            self.name,
            {"mentions": 500, "baseline_mean": 20, "baseline_std": 50},
            source_ref=f"fake://{symbol}/social",
        )


class FakeFilings:
    name = "fake-sec"

    def filings(self, symbol: str, cik: str, as_of: datetime) -> list[Observation]:
        return [
            Observation.create(
                symbol,
                "filing",
                as_of - timedelta(days=10),
                as_of - timedelta(days=10),
                "sec",
                {"form": "S-3", "text": "at-the-market sales agreement and warrants"},
                source_ref=f"https://www.sec.gov/Archives/{cik}/fixture.htm",
            )
        ]


class FakeBroker:
    name = "fake-broker"

    def inventory(self, event_id: str, symbol: str, as_of: datetime) -> BrokerSnapshot:
        return BrokerSnapshot.create(
            event_id=event_id,
            broker=self.name,
            observed_at=as_of,
            shortable=True,
            available_shares=5_000,
            locate_required=True,
            locate_cost_per_share=0.03,
            borrow_fee_annualized=0.75,
            bid=3.48,
            ask=3.52,
            margin_requirement=1.5,
            buying_power=50_000,
            rule201_active=False,
            price_test_supported=True,
            locate_expires_at=as_of + timedelta(hours=8),
        )


class FakeAccount:
    name = "fake-account"

    def risk_snapshot(self, symbol: str, as_of: datetime) -> RiskSnapshot:
        return RiskSnapshot(as_of, 100_000, 0, 0, 0, 0)


def test_realtime_monitor_orchestrates_all_sources(tmp_path) -> None:
    cutoff = datetime(2025, 1, 6, 15, tzinfo=UTC)
    with EventStore(tmp_path / "monitor.db") as store:
        monitor = RealtimeMonitor(
            ResearchService(AppConfig(), store),
            ConnectorBundle(
                market=FakeMarket(),
                broker=FakeBroker(),
                account=FakeAccount(),
                news=FakeNews(),
                social=FakeSocial(),
                filings=FakeFilings(),
            ),
        )
        result = monitor.poll_symbol("MONI", "0000123456", cutoff)
        assert result.qualified is True
        assert result.event_id and result.decision_id
        kinds = {
            item.kind for item in store.observations_as_of("MONI", cutoff, event_id=result.event_id)
        }
        assert kinds >= {"market_snapshot", "bar", "news", "social", "filing"}
        assert store.latest_broker_payload(result.event_id)["broker"] == "fake-broker"


def test_realtime_monitor_does_not_query_broker_for_non_candidate(tmp_path) -> None:
    cutoff = datetime(2025, 1, 6, 15, tzinfo=UTC)
    with EventStore(tmp_path / "monitor-no-event.db") as store:
        monitor = RealtimeMonitor(
            ResearchService(AppConfig(), store),
            ConnectorBundle(
                market=FakeMarket(price=1.6),
                broker=FakeBroker(),
                account=FakeAccount(),
            ),
        )
        result = monitor.poll_symbol("QUIET", "0000123456", cutoff)
        assert result.qualified is False
        assert result.event_id is None
        assert any("day return" in reason for reason in result.reasons)
