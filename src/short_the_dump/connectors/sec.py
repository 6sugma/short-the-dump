from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..models import Observation, ensure_utc


class SecEdgarClient:
    """Read-only SEC submissions adapter with an explicit identity and bounded request rate."""

    name = "sec"
    base_url = "https://data.sec.gov"

    def __init__(self, user_agent: str, requests_per_second: float = 5.0) -> None:
        if "@" not in user_agent:
            raise ValueError("SEC user_agent must identify an organization and contact email")
        if not 0 < requests_per_second <= 10:
            raise ValueError("SEC request rate must be in (0, 10]")
        self.user_agent = user_agent
        self.minimum_interval = 1 / requests_per_second
        self._last_request = 0.0

    def _get_json(self, url: str) -> Mapping[str, Any]:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Host": "data.sec.gov",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed SEC host
            payload = json.load(response)
        self._last_request = time.monotonic()
        return payload

    def submissions(self, cik: str) -> Mapping[str, Any]:
        normalized = re.sub(r"\D", "", cik).zfill(10)
        return self._get_json(f"{self.base_url}/submissions/CIK{normalized}.json")

    def filings(self, symbol: str, cik: str, as_of: datetime) -> Sequence[Observation]:
        payload = self.submissions(cik)
        recent = payload.get("filings", {}).get("recent", {})
        columns = {key: value for key, value in recent.items() if isinstance(value, list)}
        count = min((len(value) for value in columns.values()), default=0)
        cutoff = ensure_utc(as_of)
        result: list[Observation] = []
        for index in range(count):
            accepted_raw = str(columns.get("acceptanceDateTime", [""] * count)[index])
            if not accepted_raw:
                filed = str(columns.get("filingDate", [""] * count)[index])
                accepted_at = datetime.fromisoformat(f"{filed}T23:59:59+00:00")
            else:
                accepted_at = datetime.fromisoformat(accepted_raw.replace("Z", "+00:00"))
                if accepted_at.tzinfo is None:
                    accepted_at = accepted_at.replace(tzinfo=UTC)
            if accepted_at > cutoff:
                continue
            accession = str(columns.get("accessionNumber", [""] * count)[index])
            form = str(columns.get("form", [""] * count)[index])
            primary_document = str(columns.get("primaryDocument", [""] * count)[index])
            result.append(
                Observation.create(
                    symbol=symbol,
                    kind="filing",
                    effective_at=accepted_at,
                    observed_at=cutoff,
                    source=self.name,
                    source_ref=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_document}",
                    payload={
                        "form": form,
                        "accession": accession,
                        "filing_date": str(columns.get("filingDate", [""] * count)[index]),
                        "report_date": str(columns.get("reportDate", [""] * count)[index]),
                        "primary_document": primary_document,
                        "title": f"SEC {form} filing",
                        "text": "",
                    },
                )
            )
        return result
