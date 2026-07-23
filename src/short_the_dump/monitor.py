from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from .connectors.base import (
    AccountStateProvider,
    BrokerInventoryProvider,
    FilingProvider,
    MarketDataProvider,
    NewsProvider,
    SocialAttentionProvider,
)
from .models import Observation, StrategyKind, ensure_utc, utc_now
from .service import ResearchService


@dataclass(frozen=True, slots=True)
class ConnectorBundle:
    market: MarketDataProvider
    broker: BrokerInventoryProvider
    account: AccountStateProvider
    news: NewsProvider | None = None
    social: SocialAttentionProvider | None = None
    filings: FilingProvider | None = None


@dataclass(frozen=True, slots=True)
class MonitorResult:
    symbol: str
    polled_at: datetime
    event_id: str | None
    decision_id: str | None
    qualified: bool
    reasons: tuple[str, ...] = ()


class RealtimeMonitor:
    """Synchronous polling orchestrator; adapters own transport, auth, and entitlements."""

    def __init__(
        self,
        service: ResearchService,
        connectors: ConnectorBundle,
        *,
        news_lookback_days: int = 180,
        filing_lookback_days: int = 730,
        strategy: StrategyKind = StrategyKind.DAY_PLUS_1,
    ) -> None:
        self.service = service
        self.connectors = connectors
        self.news_lookback_days = news_lookback_days
        self.filing_lookback_days = filing_lookback_days
        self.strategy = strategy

    def poll_symbol(self, symbol: str, cik: str, as_of: datetime | None = None) -> MonitorResult:
        cutoff = ensure_utc(as_of or utc_now())
        symbol = symbol.upper()
        try:
            market = self.connectors.market.market_snapshot(symbol, cutoff)
            self._validate_observation(market, symbol, "market_snapshot")
            self.service.ingest(market)
            event, reasons = self.service.detect(market)
            if event is None:
                return MonitorResult(symbol, cutoff, None, None, False, reasons)

            session_start = datetime.combine(cutoff.date(), time.min, tzinfo=UTC)
            supporting: list[Observation] = []
            supporting.extend(self.connectors.market.bars(symbol, session_start, cutoff))
            supporting.extend(self.connectors.market.halts(symbol, session_start, cutoff))
            if self.connectors.news:
                supporting.extend(
                    self.connectors.news.news(
                        symbol, cutoff - timedelta(days=self.news_lookback_days), cutoff
                    )
                )
            if self.connectors.social:
                social = self.connectors.social.attention(symbol, cutoff)
                if social is not None:
                    supporting.append(social)
            if self.connectors.filings:
                supporting.extend(self.connectors.filings.filings(symbol, cik, cutoff))
            for observation in supporting:
                if observation.symbol != symbol:
                    raise ValueError(
                        f"provider returned {observation.symbol} while polling {symbol}"
                    )
                linked = observation
                if observation.event_id is None:
                    linked = Observation(
                        id=observation.id,
                        symbol=observation.symbol,
                        kind=observation.kind,
                        effective_at=observation.effective_at,
                        observed_at=observation.observed_at,
                        source=observation.source,
                        payload=observation.payload,
                        event_id=event.id,
                        source_ref=observation.source_ref,
                    )
                self.service.ingest(linked)

            broker = self.connectors.broker.inventory(event.id, symbol, cutoff)
            risk = self.connectors.account.risk_snapshot(symbol, cutoff)
            decision = self.service.analyze(
                event.id,
                cutoff,
                broker=broker,
                risk_snapshot=risk,
                strategy=self.strategy,
            )
            return MonitorResult(symbol, cutoff, event.id, decision.id, True)
        except Exception as exc:
            self.service.store.audit(
                "realtime_monitor",
                "monitor.poll_failed",
                "symbol",
                symbol,
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def poll_universe(
        self,
        universe: Sequence[tuple[str, str]],
        as_of: datetime | None = None,
    ) -> list[MonitorResult]:
        cutoff = ensure_utc(as_of or utc_now())
        results: list[MonitorResult] = []
        for symbol, cik in universe:
            try:
                results.append(self.poll_symbol(symbol, cik, cutoff))
            except Exception as exc:
                results.append(
                    MonitorResult(
                        symbol=symbol.upper(),
                        polled_at=cutoff,
                        event_id=None,
                        decision_id=None,
                        qualified=False,
                        reasons=(f"provider error: {type(exc).__name__}: {exc}",),
                    )
                )
        return results

    def run_forever(
        self,
        universe: Sequence[tuple[str, str]],
        *,
        interval_seconds: float = 30.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("poll interval must be at least one second")
        stopper = stop_event or threading.Event()
        while not stopper.is_set():
            self.poll_universe(universe)
            stopper.wait(interval_seconds)

    @staticmethod
    def _validate_observation(observation: Observation, symbol: str, kind: str) -> None:
        if observation.symbol != symbol or observation.kind != kind:
            raise ValueError(
                f"provider returned {observation.symbol}/{observation.kind}; expected {symbol}/{kind}"
            )
