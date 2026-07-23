from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from statistics import fmean
from typing import Any

from .config import DetectionConfig, SignalConfig
from .models import (
    CatalystAssessment,
    CatalystLabel,
    EventCase,
    FeatureSnapshot,
    MarketBar,
    Observation,
    SupplyRiskAssessment,
    clamp,
    ensure_utc,
)

TOKEN_RE = re.compile(r"[a-z0-9]+")
MATERIAL_TERMS: dict[str, float] = {
    "fda approval": 1.0,
    "fda clearance": 0.95,
    "definitive agreement": 0.85,
    "merger agreement": 0.90,
    "acquisition": 0.70,
    "patent granted": 0.55,
    "purchase order": 0.55,
    "contract award": 0.65,
    "commercial launch": 0.60,
    "positive phase": 0.70,
    "strategic investment": 0.55,
    "revenue guidance": 0.65,
}
LOW_MATERIALITY_TERMS = {
    "conference participation",
    "fireside chat",
    "investor presentation",
    "letter to shareholders",
    "corporate update",
    "webinar",
    "brand ambassador",
    "memorandum of understanding",
    "non-binding",
}
PROMO_TERMS = {
    "breakthrough opportunity",
    "massive market",
    "game changer",
    "undervalued",
    "next generation",
    "revolutionary",
    "transformational",
    "paid awareness",
    "sponsored content",
    "stock promotion",
    "newsletter",
    "limited time",
}
HIGH_CREDIBILITY_SOURCES = {
    "sec",
    "fda",
    "uspto",
    "clinicaltrials.gov",
    "nasdaq",
    "nyse",
}
MID_CREDIBILITY_SOURCES = {
    "reuters",
    "associated press",
    "business wire",
    "globe newswire",
    "pr newswire",
    "accesswire",
}


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def _jaccard(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _text(payload: Mapping[str, Any]) -> str:
    return " ".join(
        str(payload.get(key, "")) for key in ("headline", "title", "summary", "body", "text")
    ).strip()


class CatalystClassifier:
    """Transparent heuristic classifier; scores are evidence ratings, not probabilities."""

    def classify(
        self,
        news: Sequence[Observation],
        filings: Sequence[Observation],
        day0: date,
    ) -> CatalystAssessment:
        current: list[Observation] = []
        history: list[Observation] = []
        boundary = (
            datetime.combine(
                day0, datetime.min.time(), tzinfo=ensure_utc(news[0].effective_at).tzinfo
            )
            if news
            else None
        )
        for item in [*news, *filings]:
            if item.effective_at.date() >= day0:
                current.append(item)
            elif boundary is None or item.effective_at < boundary:
                history.append(item)
        if not current:
            return CatalystAssessment(
                label=CatalystLabel.NONE,
                novelty=0.0,
                credibility=0.0,
                materiality=0.0,
                promotional_risk=0.0,
                confidence=0.0,
                reasons=("No point-in-time catalyst was available by the evaluation cutoff.",),
            )

        current_texts = [_text(item.payload) for item in current if _text(item.payload)]
        historical_texts = [_text(item.payload) for item in history if _text(item.payload)]
        combined = " ".join(current_texts).lower()

        max_similarity = 0.0
        for current_text in current_texts:
            for old_text in historical_texts:
                max_similarity = max(max_similarity, _jaccard(current_text, old_text))
        novelty = clamp(1.0 - max_similarity, 0.0, 1.0)

        credibility_values: list[float] = []
        source_refs: list[str] = []
        reasons: list[str] = []
        for item in current:
            source = item.source.lower()
            if item.kind == "filing" or source in HIGH_CREDIBILITY_SOURCES:
                credibility_values.append(0.95)
            elif source in MID_CREDIBILITY_SOURCES:
                credibility_values.append(0.70)
            elif bool(item.payload.get("issuer_press_release")):
                credibility_values.append(0.55)
            else:
                credibility_values.append(0.35)
            if item.source_ref:
                source_refs.append(item.source_ref)
        credibility = max(credibility_values, default=0.0)

        material_hits = [
            (term, weight) for term, weight in MATERIAL_TERMS.items() if term in combined
        ]
        low_hits = sorted(term for term in LOW_MATERIALITY_TERMS if term in combined)
        if material_hits:
            materiality = max(weight for _, weight in material_hits)
            reasons.append(
                "Material terms: " + ", ".join(term for term, _ in material_hits[:4]) + "."
            )
        elif low_hits:
            materiality = 0.15
            reasons.append("Low-materiality language: " + ", ".join(low_hits[:4]) + ".")
        else:
            materiality = 0.35
            reasons.append("No independently high-materiality term was identified.")

        # Numeric contract or financing values can raise materiality only when source data states them.
        stated_values = [
            float(item.payload["stated_value"])
            for item in current
            if isinstance(item.payload.get("stated_value"), (int, float))
            and float(item.payload["stated_value"]) > 0
        ]
        if stated_values:
            materiality = max(materiality, 0.65)
            reasons.append("The catalyst includes a stated economic value.")

        promo_hits = sorted(term for term in PROMO_TERMS if term in combined)
        exclamations = combined.count("!")
        promo_risk = clamp(len(promo_hits) * 0.18 + min(exclamations, 4) * 0.05, 0.0, 1.0)
        if any(bool(item.payload.get("paid_promotion")) for item in current):
            promo_risk = max(promo_risk, 0.95)
            reasons.append("A source explicitly marked paid promotion.")
        if promo_hits:
            reasons.append("Promotional language: " + ", ".join(promo_hits[:4]) + ".")
        if max_similarity >= 0.65:
            reasons.append(
                f"Current language closely resembles earlier coverage ({max_similarity:.0%})."
            )
        else:
            reasons.append(f"Catalyst text novelty rating is {novelty:.0%}.")

        confidence = clamp(
            0.5 * credibility + 0.3 * min(len(current) / 2, 1) + 0.2 * bool(current_texts), 0, 1
        )
        if promo_risk >= 0.65:
            label = CatalystLabel.PROMOTIONAL
        elif materiality >= 0.55 and novelty >= 0.45:
            label = CatalystLabel.MATERIAL_NOVEL
        elif materiality >= 0.55:
            label = CatalystLabel.MATERIAL_REHASH
        else:
            label = CatalystLabel.LOW_MATERIALITY
        return CatalystAssessment(
            label=label,
            novelty=novelty,
            credibility=credibility,
            materiality=materiality,
            promotional_risk=promo_risk,
            confidence=confidence,
            reasons=tuple(reasons),
            source_refs=tuple(dict.fromkeys(source_refs)),
        )


class SupplyRiskAnalyzer:
    DILUTION_FORMS = {"S-1", "S-3", "F-1", "F-3", "424B3", "424B4", "424B5", "EFFECT"}
    OFFERING_TERMS = {
        "at-the-market": 0.32,
        "sales agreement": 0.25,
        "equity line": 0.30,
        "standby equity": 0.30,
        "resale prospectus": 0.25,
        "registered direct": 0.28,
        "public offering": 0.22,
        "warrant": 0.16,
        "convertible": 0.20,
        "variable price": 0.30,
        "toxic": 0.35,
    }

    def assess(
        self,
        filings: Sequence[Observation],
        capital: Observation | None,
        as_of: datetime,
    ) -> SupplyRiskAssessment:
        text = " ".join(_text(item.payload).lower() for item in filings)
        forms = {str(item.payload.get("form", "")).upper() for item in filings}
        score = 0.0
        reasons: list[str] = []
        refs: list[str] = []
        dilution_forms = sorted(forms & self.DILUTION_FORMS)
        if dilution_forms:
            score += min(0.35, 0.12 * len(dilution_forms))
            reasons.append("Potential supply filings on record: " + ", ".join(dilution_forms) + ".")
        for term, weight in self.OFFERING_TERMS.items():
            if term in text:
                score += weight
                reasons.append(f"Filing language mentions {term}.")
        for item in filings:
            if item.source_ref:
                refs.append(item.source_ref)

        atm_active = "at-the-market" in text or "sales agreement" in text
        shelf_active = bool(dilution_forms) and any(
            str(item.payload.get("status", "active")).lower() not in {"withdrawn", "expired"}
            for item in filings
            if str(item.payload.get("form", "")).upper() in self.DILUTION_FORMS
        )
        warrants = "warrant" in text or "convertible" in text
        reverse_split_recent = False
        overhang_ratio: float | None = None
        if capital:
            payload = capital.payload
            split_date_raw = payload.get("last_reverse_split_date")
            if split_date_raw:
                try:
                    split_date = date.fromisoformat(str(split_date_raw))
                    reverse_split_recent = (as_of.date() - split_date).days <= 365
                except ValueError:
                    pass
            authorized = payload.get("authorized_shares")
            outstanding = payload.get("shares_outstanding")
            float_shares = payload.get("float_shares")
            if all(
                isinstance(value, (int, float)) for value in (authorized, outstanding, float_shares)
            ):
                denominator = max(float(float_shares), 1.0)
                overhang_ratio = max((float(authorized) - float(outstanding)) / denominator, 0.0)
                score += clamp(overhang_ratio / 10, 0, 0.35)
                if overhang_ratio >= 2:
                    reasons.append(
                        f"Authorized but unissued capacity is about {overhang_ratio:.1f}x float."
                    )
            remaining = payload.get("registered_offering_remaining")
            market_cap = payload.get("market_cap")
            if (
                isinstance(remaining, (int, float))
                and isinstance(market_cap, (int, float))
                and market_cap > 0
            ):
                ratio = float(remaining) / float(market_cap)
                score += clamp(ratio, 0, 0.35)
                reasons.append(f"Reported offering capacity is {ratio:.0%} of market cap.")
            if reverse_split_recent:
                score += 0.10
                reasons.append("A reverse split was reported within the prior year.")
        return SupplyRiskAssessment(
            score=clamp(score, 0, 1),
            shelf_or_resale_active=shelf_active,
            atm_active=atm_active,
            warrants_or_convertibles=warrants,
            reverse_split_recent=reverse_split_recent,
            authorized_overhang_ratio=overhang_ratio,
            reasons=tuple(dict.fromkeys(reasons)),
            filing_refs=tuple(dict.fromkeys(refs)),
        )


class ExtremeMoveDetector:
    def __init__(self, config: DetectionConfig) -> None:
        self.config = config

    def evaluate(self, snapshot: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        exchange = str(snapshot.get("exchange", "")).upper()
        price = float(snapshot.get("price", 0) or 0)
        previous_close = float(snapshot.get("previous_close", 0) or 0)
        volume = float(snapshot.get("cumulative_volume", 0) or 0)
        average_volume = float(snapshot.get("average_daily_volume", 0) or 0)
        float_shares = snapshot.get("float_shares")
        market_cap = snapshot.get("market_cap")
        day_return = price / previous_close - 1 if previous_close > 0 else -1
        relative_volume = volume / average_volume if average_volume > 0 else 0
        dollar_volume = float(snapshot.get("dollar_volume", price * volume) or 0)
        checks = (
            (exchange in self.config.allowed_exchanges, f"exchange {exchange} is not eligible"),
            (
                self.config.min_price <= price <= self.config.max_price,
                f"price {price:.2f} is outside bounds",
            ),
            (
                isinstance(float_shares, (int, float))
                and 0 < float(float_shares) <= self.config.max_float_shares,
                "float is missing or above limit",
            ),
            (
                market_cap is None or float(market_cap) <= self.config.max_market_cap,
                "market cap is above limit",
            ),
            (
                day_return >= self.config.min_day0_return,
                f"day return {day_return:.1%} is below threshold",
            ),
            (
                relative_volume >= self.config.min_relative_volume,
                f"relative volume {relative_volume:.1f}x is below threshold",
            ),
            (
                dollar_volume >= self.config.min_dollar_volume,
                f"dollar volume ${dollar_volume:,.0f} is below threshold",
            ),
        )
        reasons.extend(reason for passed, reason in checks if not passed)
        return not reasons, tuple(reasons)


class FeatureEngine:
    def __init__(self, config: SignalConfig) -> None:
        self.config = config
        self.catalysts = CatalystClassifier()
        self.supply = SupplyRiskAnalyzer()

    def build(
        self,
        event: EventCase,
        observations: Sequence[Observation],
        as_of: datetime,
    ) -> FeatureSnapshot:
        as_of = ensure_utc(as_of)
        visible = [
            item
            for item in observations
            if ensure_utc(item.observed_at) <= as_of and ensure_utc(item.effective_at) <= as_of
        ]
        by_kind: dict[str, list[Observation]] = defaultdict(list)
        for item in visible:
            by_kind[item.kind].append(item)
        snapshots = by_kind.get("market_snapshot", [])
        if not snapshots:
            raise ValueError("a point-in-time market_snapshot observation is required")
        market = max(snapshots, key=lambda item: (item.observed_at, item.effective_at))
        payload = market.payload
        bars = sorted(
            (self._bar_from_observation(item) for item in by_kind.get("bar", [])),
            key=lambda bar: bar.timestamp,
        )
        bars = [
            bar
            for bar in bars
            if bar.timestamp.date() == event.day0_date and bar.timestamp <= as_of
        ]

        price = float(payload.get("price", bars[-1].close if bars else event.detection_price))
        previous_close = float(payload.get("previous_close", event.baseline_close))
        cumulative_volume = float(payload.get("cumulative_volume", sum(bar.volume for bar in bars)))
        average_volume = float(payload.get("average_daily_volume", 0) or 0)
        relative_volume = cumulative_volume / average_volume if average_volume > 0 else 0.0
        dollar_volume = float(payload.get("dollar_volume", cumulative_volume * price))
        float_value = payload.get("float_shares")
        float_shares = (
            int(float_value) if isinstance(float_value, (int, float)) and float_value > 0 else None
        )
        turnover = cumulative_volume / float_shares if float_shares else None
        market_cap_value = payload.get("market_cap")
        market_cap = float(market_cap_value) if isinstance(market_cap_value, (int, float)) else None

        session_high = max([float(payload.get("session_high", price)), *(bar.high for bar in bars)])
        close_off_high = (
            clamp((session_high - price) / session_high, 0, 1) if session_high > 0 else 0
        )
        current_vwap = self._current_vwap(bars, payload)
        vwap_distance = price / current_vwap - 1 if current_vwap and current_vwap > 0 else None
        lower_high_ratio = self._lower_high_ratio(bars)
        failed_reclaims = self._failed_vwap_reclaims(bars)
        opening_breakdown = self._opening_range_breakdown(bars)
        volume_fade = self._volume_fade(bars)
        halts = len(by_kind.get("halt", [])) + sum(1 for bar in bars if bar.halted)
        social_z = self._social_zscore(by_kind.get("social", []))
        catalyst = self.catalysts.classify(
            by_kind.get("news", []), by_kind.get("filing", []), event.day0_date
        )
        capital = max(
            by_kind.get("capital_structure", []), key=lambda item: item.observed_at, default=None
        )
        supply = self.supply.assess(by_kind.get("filing", []), capital, as_of)

        momentum_components = {
            "below_vwap": clamp(-(vwap_distance or 0) / 0.08, 0, 1),
            "off_high": clamp(close_off_high / 0.35, 0, 1),
            "lower_highs": lower_high_ratio,
            "failed_reclaims": clamp(failed_reclaims / 3, 0, 1),
            "opening_breakdown": float(opening_breakdown),
            "volume_fade": clamp(
                (1 - (volume_fade if volume_fade is not None else 1)) / 0.75, 0, 1
            ),
        }
        momentum = 100 * (
            momentum_components["below_vwap"] * 0.22
            + momentum_components["off_high"] * 0.18
            + momentum_components["lower_highs"] * 0.18
            + momentum_components["failed_reclaims"] * 0.17
            + momentum_components["opening_breakdown"] * 0.15
            + momentum_components["volume_fade"] * 0.10
        )
        evidence = self._evidence(
            vwap_distance,
            close_off_high,
            lower_high_ratio,
            failed_reclaims,
            opening_breakdown,
            volume_fade,
            turnover,
            halts,
            social_z,
            catalyst,
            supply,
        )
        freshness = max((as_of - market.observed_at).total_seconds(), 0.0)
        return FeatureSnapshot.create(
            event_id=event.id,
            as_of=as_of,
            price=price,
            day0_return=price / previous_close - 1 if previous_close > 0 else 0.0,
            relative_volume=relative_volume,
            dollar_volume=dollar_volume,
            float_shares=float_shares,
            float_turnover=turnover,
            market_cap=market_cap,
            vwap_distance=vwap_distance,
            close_off_high=close_off_high,
            lower_high_ratio=lower_high_ratio,
            failed_vwap_reclaims=failed_reclaims,
            opening_range_breakdown=opening_breakdown,
            volume_fade_ratio=volume_fade,
            halt_count=halts,
            social_attention_zscore=social_z,
            catalyst=catalyst,
            supply_risk=supply,
            momentum_failure_score=clamp(momentum, 0, 100),
            data_freshness_seconds=freshness,
            evidence=tuple(evidence),
        )

    def reversal_score(self, snapshot: FeatureSnapshot) -> float:
        catalyst_fragility = clamp(
            (1 - snapshot.catalyst.materiality) * 0.45
            + (1 - snapshot.catalyst.novelty) * 0.20
            + snapshot.catalyst.promotional_risk * 0.35,
            0,
            1,
        )
        exhaustion = clamp(
            ((snapshot.float_turnover or 0) / 5) * 0.55
            + clamp((snapshot.relative_volume - 5) / 20, 0, 1) * 0.25
            + clamp(snapshot.close_off_high / 0.4, 0, 1) * 0.20,
            0,
            1,
        )
        # Halts increase gap/squeeze risk and therefore reduce actionability even when failure evidence is strong.
        halt_penalty = min(snapshot.halt_count * 2.5, 12.5)
        score = (
            snapshot.momentum_failure_score * 0.48
            + catalyst_fragility * 100 * 0.20
            + snapshot.supply_risk.score * 100 * 0.20
            + exhaustion * 100 * 0.12
            - halt_penalty
        )
        return clamp(score, 0, 100)

    def _bar_from_observation(self, item: Observation) -> MarketBar:
        payload = item.payload
        return MarketBar(
            symbol=item.symbol,
            timestamp=item.effective_at,
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            volume=int(payload.get("volume", 0)),
            vwap=float(payload["vwap"]) if payload.get("vwap") is not None else None,
            bid=float(payload["bid"]) if payload.get("bid") is not None else None,
            ask=float(payload["ask"]) if payload.get("ask") is not None else None,
            halted=bool(payload.get("halted", False)),
            session_index=int(payload.get("session_index", 0)),
        )

    @staticmethod
    def _current_vwap(bars: Sequence[MarketBar], payload: Mapping[str, Any]) -> float | None:
        if payload.get("vwap") is not None:
            return float(payload["vwap"])
        if bars and bars[-1].vwap is not None:
            return bars[-1].vwap
        volume = sum(bar.volume for bar in bars)
        if volume <= 0:
            return None
        return sum(((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in bars) / volume

    def _lower_high_ratio(self, bars: Sequence[MarketBar]) -> float:
        selected = list(bars[-self.config.lower_high_lookback_bars :])
        if len(selected) < 3:
            return 0.0
        comparisons = [
            right.high < left.high for left, right in zip(selected, selected[1:], strict=False)
        ]
        return sum(comparisons) / len(comparisons)

    def _failed_vwap_reclaims(self, bars: Sequence[MarketBar]) -> int:
        tolerance = self.config.failed_reclaim_tolerance_bps / 10_000
        failures = 0
        for bar in bars:
            if bar.vwap and bar.high >= bar.vwap * (1 - tolerance) and bar.close < bar.vwap:
                failures += 1
        return failures

    def _opening_range_breakdown(self, bars: Sequence[MarketBar]) -> bool:
        active = [bar for bar in bars if not bar.halted]
        if len(active) <= self.config.opening_range_minutes:
            return False
        opening = active[: self.config.opening_range_minutes]
        opening_low = min(bar.low for bar in opening)
        return any(bar.close < opening_low for bar in active[self.config.opening_range_minutes :])

    @staticmethod
    def _volume_fade(bars: Sequence[MarketBar]) -> float | None:
        active = [bar for bar in bars if not bar.halted and bar.volume > 0]
        if len(active) < 6:
            return None
        chunk = max(3, len(active) // 4)
        early = fmean(bar.volume for bar in active[:chunk])
        late = fmean(bar.volume for bar in active[-chunk:])
        return late / early if early > 0 else None

    @staticmethod
    def _social_zscore(items: Sequence[Observation]) -> float | None:
        if not items:
            return None
        latest = max(items, key=lambda item: item.observed_at).payload
        mentions = latest.get("mentions")
        mean = latest.get("baseline_mean")
        std = latest.get("baseline_std")
        if (
            all(isinstance(value, (int, float)) for value in (mentions, mean, std))
            and float(std) > 0
        ):
            return (float(mentions) - float(mean)) / float(std)
        return float(latest["zscore"]) if isinstance(latest.get("zscore"), (int, float)) else None

    @staticmethod
    def _evidence(
        vwap_distance: float | None,
        off_high: float,
        lower_highs: float,
        failed_reclaims: int,
        opening_breakdown: bool,
        volume_fade: float | None,
        turnover: float | None,
        halts: int,
        social_z: float | None,
        catalyst: CatalystAssessment,
        supply: SupplyRiskAssessment,
    ) -> list[str]:
        evidence: list[str] = []
        if vwap_distance is not None:
            evidence.append(f"Price is {vwap_distance:+.1%} versus session VWAP.")
        evidence.append(f"Price is {off_high:.1%} below the observed session high.")
        if lower_highs:
            evidence.append(
                f"{lower_highs:.0%} of recent high-to-high comparisons are lower highs."
            )
        if failed_reclaims:
            evidence.append(f"Observed {failed_reclaims} failed VWAP reclaim bar(s).")
        if opening_breakdown:
            evidence.append("Price closed below the initial opening range after it formed.")
        if volume_fade is not None:
            evidence.append(f"Late-bar volume is {volume_fade:.2f}x early-bar volume.")
        if turnover is not None:
            evidence.append(f"Reported volume equals approximately {turnover:.1f}x reported float.")
        if halts:
            evidence.append(
                f"Observed {halts} trading halt marker(s); gap and fill risk are elevated."
            )
        if social_z is not None:
            evidence.append(
                f"Social attention is {social_z:.1f} standard deviations above baseline."
            )
        evidence.append(
            f"Catalyst classified {catalyst.label.value}: novelty {catalyst.novelty:.0%}, "
            f"credibility {catalyst.credibility:.0%}, materiality {catalyst.materiality:.0%}."
        )
        if supply.reasons:
            evidence.append(f"Share-supply risk rating is {supply.score:.0%}.")
        return evidence


def cosine_like_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """Small dependency-free similarity measure for historical analog ranking."""
    fields = (
        ("day0_return", 1.0),
        ("relative_volume", 0.12),
        ("float_turnover", 0.30),
        ("close_off_high", 3.0),
        ("lower_high_ratio", 2.0),
        ("momentum_failure_score", 0.03),
    )
    distance = 0.0
    present = 0
    for name, scale in fields:
        a, b = left.get(name), right.get(name)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            distance += ((float(a) - float(b)) * scale) ** 2
            present += 1
    if not present:
        return 0.0
    return 1 / (1 + math.sqrt(distance / present))
