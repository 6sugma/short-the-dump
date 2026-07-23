from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from ..models import BrokerSnapshot, Observation, RiskSnapshot


class MarketDataProvider(Protocol):
    name: str

    def market_snapshot(self, symbol: str, as_of: datetime) -> Observation: ...

    def bars(self, symbol: str, start: datetime, end: datetime) -> Sequence[Observation]: ...

    def halts(self, symbol: str, start: datetime, end: datetime) -> Sequence[Observation]: ...


class NewsProvider(Protocol):
    name: str

    def news(self, symbol: str, start: datetime, end: datetime) -> Sequence[Observation]: ...


class SocialAttentionProvider(Protocol):
    name: str

    def attention(self, symbol: str, as_of: datetime) -> Observation | None: ...


class FilingProvider(Protocol):
    name: str

    def filings(self, symbol: str, cik: str, as_of: datetime) -> Sequence[Observation]: ...


class BrokerInventoryProvider(Protocol):
    name: str

    def inventory(self, event_id: str, symbol: str, as_of: datetime) -> BrokerSnapshot: ...


class AccountStateProvider(Protocol):
    name: str

    def risk_snapshot(self, symbol: str, as_of: datetime) -> RiskSnapshot: ...
