from __future__ import annotations

import dataclasses

import pytest

from short_the_dump.config import AppConfig, load_config
from short_the_dump.models import OperationalMode


def test_default_config_is_alert_only_and_safe() -> None:
    config = load_config()
    assert config.runtime.mode is OperationalMode.ALERT_ONLY
    assert config.runtime.manual_approval_required is True
    assert config.runtime.live_order_routing_enabled is False
    assert config.risk.no_averaging_into_losers is True


def test_live_mode_requires_deployment_interlock(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig()
    live = dataclasses.replace(
        config,
        runtime=dataclasses.replace(
            config.runtime,
            mode=OperationalMode.LIMITED_LIVE,
            live_order_routing_enabled=True,
        ),
    )
    monkeypatch.delenv("SHORT_THE_DUMP_LIVE_ACK", raising=False)
    with pytest.raises(ValueError, match="interlock"):
        live.validate()
    monkeypatch.setenv("SHORT_THE_DUMP_LIVE_ACK", "I_UNDERSTAND_LIVE_SHORT_RISK")
    assert live.validate() is live


def test_manual_approval_and_no_averaging_are_invariants() -> None:
    config = AppConfig()
    with pytest.raises(ValueError, match="manual approval"):
        dataclasses.replace(
            config, runtime=dataclasses.replace(config.runtime, manual_approval_required=False)
        ).validate()
    with pytest.raises(ValueError, match="no_averaging"):
        dataclasses.replace(
            config, risk=dataclasses.replace(config.risk, no_averaging_into_losers=False)
        ).validate()
