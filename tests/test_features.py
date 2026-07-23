from __future__ import annotations

from datetime import UTC, datetime, timedelta

from short_the_dump.config import DetectionConfig, SignalConfig
from short_the_dump.features import (
    CatalystClassifier,
    ExtremeMoveDetector,
    FeatureEngine,
    SupplyRiskAnalyzer,
)
from short_the_dump.models import CatalystLabel, Observation


def test_extreme_move_detector_enforces_listing_float_move_and_volume() -> None:
    detector = ExtremeMoveDetector(DetectionConfig())
    payload = {
        "exchange": "NASDAQ",
        "price": 4.0,
        "previous_close": 2.0,
        "cumulative_volume": 10_000_000,
        "average_daily_volume": 500_000,
        "float_shares": 3_000_000,
        "market_cap": 50_000_000,
    }
    assert detector.evaluate(payload)[0] is True
    payload["exchange"] = "OTC"
    qualifies, reasons = detector.evaluate(payload)
    assert qualifies is False
    assert any("exchange" in reason for reason in reasons)


def test_catalyst_classifier_detects_rehash_and_promotion() -> None:
    day0 = datetime(2025, 1, 6, tzinfo=UTC)
    old = Observation.create(
        "TEST",
        "news",
        day0 - timedelta(days=30),
        day0 - timedelta(days=30),
        "issuer",
        {"headline": "Revolutionary next generation platform is a game changer"},
        source_ref="old",
    )
    current = Observation.create(
        "TEST",
        "news",
        day0,
        day0,
        "issuer",
        {
            "headline": "Revolutionary next generation platform is a game changer!",
            "paid_promotion": True,
        },
        source_ref="new",
    )
    result = CatalystClassifier().classify([old, current], [], day0.date())
    assert result.label is CatalystLabel.PROMOTIONAL
    assert result.novelty < 0.25
    assert result.promotional_risk >= 0.95


def test_supply_risk_uses_only_visible_filings() -> None:
    as_of = datetime(2025, 1, 6, 15, tzinfo=UTC)
    filing = Observation.create(
        "TEST",
        "filing",
        as_of - timedelta(days=5),
        as_of - timedelta(days=5),
        "sec",
        {"form": "S-3", "text": "at-the-market sales agreement with warrants", "status": "active"},
        source_ref="sec://s3",
    )
    capital = Observation.create(
        "TEST",
        "capital_structure",
        as_of - timedelta(days=2),
        as_of - timedelta(days=1),
        "vendor",
        {
            "authorized_shares": 100_000_000,
            "shares_outstanding": 10_000_000,
            "float_shares": 5_000_000,
        },
        source_ref="cap",
    )
    result = SupplyRiskAnalyzer().assess([filing], capital, as_of)
    assert result.atm_active is True
    assert result.warrants_or_convertibles is True
    assert result.score > 0.7


def test_feature_engine_computes_observable_failure(event) -> None:
    as_of = event.detected_at
    observations = [
        Observation.create(
            event.symbol,
            "market_snapshot",
            as_of,
            as_of,
            "vendor",
            {
                "price": 3.0,
                "previous_close": 1.5,
                "session_high": 5.0,
                "vwap": 4.0,
                "cumulative_volume": 9_000_000,
                "average_daily_volume": 500_000,
                "float_shares": 2_000_000,
                "market_cap": 30_000_000,
            },
            event_id=event.id,
        ),
        Observation.create(
            event.symbol,
            "news",
            as_of - timedelta(hours=1),
            as_of - timedelta(hours=1),
            "business wire",
            {"headline": "Company schedules investor webinar", "issuer_press_release": True},
            event_id=event.id,
        ),
    ]
    for index in range(8):
        timestamp = as_of - timedelta(minutes=7 - index)
        high = 4.2 - index * 0.12
        close = high - 0.18
        observations.append(
            Observation.create(
                event.symbol,
                "bar",
                timestamp,
                timestamp,
                "vendor",
                {
                    "open": high - 0.05,
                    "high": high,
                    "low": close - 0.08,
                    "close": close,
                    "volume": 100_000 - index * 8_000,
                    "vwap": 4.0,
                    "session_index": 0,
                },
                event_id=event.id,
            )
        )
    engine = FeatureEngine(SignalConfig(opening_range_minutes=3, lower_high_lookback_bars=5))
    feature = engine.build(event, observations, as_of)
    assert feature.vwap_distance < 0
    assert feature.lower_high_ratio == 1.0
    assert feature.failed_vwap_reclaims > 0
    assert feature.opening_range_breakdown is True
    assert feature.float_turnover == 4.5
    assert feature.momentum_failure_score > 60


def test_future_news_is_excluded_from_feature_snapshot(event) -> None:
    as_of = event.detected_at
    observations = [
        Observation.create(
            event.symbol,
            "market_snapshot",
            as_of,
            as_of,
            "vendor",
            {
                "price": 4.0,
                "previous_close": 2.0,
                "session_high": 4.5,
                "vwap": 4.0,
                "cumulative_volume": 8_000_000,
                "average_daily_volume": 500_000,
                "float_shares": 3_000_000,
            },
            event_id=event.id,
        ),
        Observation.create(
            event.symbol,
            "news",
            as_of + timedelta(hours=1),
            as_of + timedelta(hours=1),
            "fda",
            {"headline": "FDA approval"},
            event_id=event.id,
        ),
    ]
    feature = FeatureEngine(SignalConfig()).build(event, observations, as_of)
    assert feature.catalyst.label is CatalystLabel.NONE
