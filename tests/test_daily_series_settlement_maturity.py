from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "collect_market_data_entrypoint",
    ROOT / "scripts" / "collect_market_data.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ET = ZoneInfo("America/New_York")


def config() -> dict:
    return yaml.safe_load((ROOT / "config/market-data-store.yaml").read_text(encoding="utf-8"))


def test_current_et_date_before_maturity_is_blocked() -> None:
    mature, reason = MODULE.daily_series_settlement_mature(
        trade_date="2026-08-18",
        config=config(),
        now_et=datetime(2026, 8, 18, 6, 59, tzinfo=ET),
    )
    assert mature is False
    assert reason == "provider_settlement_not_mature_before_07:00_ET"


def test_current_et_date_at_maturity_is_allowed() -> None:
    mature, reason = MODULE.daily_series_settlement_mature(
        trade_date="2026-08-18",
        config=config(),
        now_et=datetime(2026, 8, 18, 7, 0, tzinfo=ET),
    )
    assert mature is True
    assert reason is None


def test_past_date_maintenance_run_is_already_mature() -> None:
    mature, reason = MODULE.daily_series_settlement_mature(
        trade_date="2026-08-17",
        config=config(),
        now_et=datetime(2026, 8, 18, 1, 0, tzinfo=ET),
    )
    assert mature is True
    assert reason is None


def test_future_date_is_rejected() -> None:
    mature, reason = MODULE.daily_series_settlement_mature(
        trade_date="2026-08-19",
        config=config(),
        now_et=datetime(2026, 8, 18, 8, 0, tzinfo=ET),
    )
    assert mature is False
    assert reason == "trade_date_is_in_the_future"


def test_schedule_and_contract_leave_buffer_before_premarket() -> None:
    cfg = config()
    assert cfg["collector"]["twelve_data_daily"]["settlement_maturity"]["minimum_next_day_et"] == "07:00"
    assert cfg["collector"]["schedules_et"]["previous_session_eod"] == "07:05"
    assert cfg["collector"]["schedules_et"]["premarket_shadow_probe"] == "07:20"
