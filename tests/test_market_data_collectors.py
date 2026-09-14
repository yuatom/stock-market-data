import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import market_data_collectors as collectors  # noqa: E402
from market_data_store import read_daily_series, write_capture, write_snapshot  # noqa: E402


def _watchlist(core):
    return {
        "core_watchlist": list(core),
        "instruments": {
            "AMD": {"asset_class": "stocks"},
            "NVDA": {"asset_class": "stocks"},
            "SPCX": {
                "asset_class": "stocks",
                "ticker_effective_at": "2026-06-12",
            },
        },
    }


def _completeness(benchmarks=(), sectors=()):
    return {
        "minute_matrix_requirements": {"tracked_benchmarks": list(benchmarks)},
        "sector_close_capability": {"exact_sector_etfs": list(sectors)},
    }


def _store_config():
    return {
        "collector": {
            "budgets": {
                "twelve_data_basic": {
                    "hard_credits_per_day": 200,
                    "hard_credits_per_minute": 8,
                }
            },
            "twelve_data_daily": {
                "history_outputsize": 64,
                "bootstrap_minimum_records": 20,
            },
        }
    }


def _access():
    return {
        "http": {"timeout_seconds": 20, "user_agent": "test"},
        "nasdaq_public_intraday": {
            "regular_session_endpoint_template": "https://example.invalid/{symbol}/{asset_class}",
            "max_workers": 4,
        },
        "twelve_data_basic": {
            "base_url": "https://example.invalid",
            "time_series_path": "/time_series",
            "interval_daily": "1day",
        },
    }


def test_daily_universe_combines_core_benchmarks_and_sectors_without_duplicates():
    universe = collectors._daily_universe(
        _watchlist(["AMD"]),
        _completeness(benchmarks=["SPY", "QQQ"], sectors=["XLK", "SPY"]),
    )
    assert {symbol for symbol, _asset in universe} == {"AMD", "SPY", "QQQ", "XLK"}


def test_twelve_bootstrap_filters_pre_identity_rows_and_builds_provider_affine_series(tmp_path, monkeypatch):
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "test-key")
    monkeypatch.setattr(collectors, "wait_for_twelve_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(collectors, "consume_twelve_credit", lambda *args, **kwargs: None)

    rows = []
    for day in range(1, 31):
        month = 6 if day <= 20 else 7
        dom = day if day <= 20 else day - 20
        date = f"2026-{month:02d}-{dom:02d}"
        rows.append(
            {
                "trade_date": date,
                "open": float(day),
                "high": float(day) + 1,
                "low": float(day) - 1,
                "close": float(day) + 0.5,
                "volume": float(day * 100),
            }
        )

    monkeypatch.setattr(
        collectors,
        "fetch_twelve_daily",
        lambda symbol, api_key, access, outputsize: rows,
    )

    result = collectors.collect_previous_session_eod(
        trade_date="2026-08-14",
        store_root=tmp_path,
        watchlist=_watchlist(["SPCX"]),
        completeness=_completeness(),
        config=_store_config(),
        access=_access(),
    )
    assert result["bootstrapped_series"] == 1
    series = read_daily_series(tmp_path, provider="twelve_data_basic", symbol="SPCX")
    assert len(series) >= 20
    assert series[0]["trade_date"] >= "2026-06-12"


def test_zero_qualified_intraday_facts_do_not_create_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collectors,
        "fetch_nasdaq_regular",
        lambda *args, **kwargs: [],
    )
    result = collectors.collect_regular_window(
        mode="open_15m",
        stage=None,
        trade_date="2026-08-14",
        start_et="09:30",
        end_et="09:45",
        store_root=tmp_path,
        watchlist=_watchlist(["AMD"]),
        completeness=_completeness(),
        access=_access(),
    )
    assert result["status"] == "no_new_qualified_facts"
    assert result["snapshot_written"] is False
    assert not (tmp_path / "snapshots/2026-08/2026-08-14/open_15m/latest.json").exists()


def test_close_first_pass_inherits_open60_and_retry_inherits_close(tmp_path, monkeypatch):
    prior_rel, prior_blob = write_capture(
        tmp_path,
        trade_date="2026-08-14",
        session="regular",
        provider="nasdaq_public_intraday",
        capture_id="open60-amd",
        generated_at="2026-08-14T10:31:00-04:00",
        actual_data_cutoff="2026-08-14T10:29:00-04:00",
        window={"start": "10:00", "end": "10:30"},
        feed_scope="nasdaq_public_chart_last_sale_volume_v1",
        qualified_facts=[
            {
                "symbol": "AMD",
                "session": "regular",
                "event_time": "2026-08-14T10:29:00-04:00",
                "source_timestamp": "2026-08-14T10:29:00-04:00",
                "last_sale": 100.0,
                "reported_volume": 10.0,
            }
        ],
    )
    write_snapshot(
        tmp_path,
        stage="open_60m",
        trade_date="2026-08-14",
        snapshot_id="open60-prior",
        generated_at="2026-08-14T10:31:05-04:00",
        data_refs=[{"path": prior_rel, "blob_sha": prior_blob, "kind": "regular_intraday_capture"}],
        coverage={},
        missing=["NVDA"],
        target_window={"start": "09:30", "end": "10:30"},
        actual_data_cutoff="2026-08-14T10:29:00-04:00",
    )

    def first_fetch(symbol, *_args, **_kwargs):
        if symbol == "AMD":
            return [
                {
                    "symbol": "AMD",
                    "asset_class": "stocks",
                    "session": "regular",
                    "event_time": "2026-08-14T15:59:00-04:00",
                    "source_timestamp": "2026-08-14T15:59:00-04:00",
                    "last_sale": 101.0,
                    "reported_volume": 20.0,
                }
            ]
        return []

    monkeypatch.setattr(collectors, "fetch_nasdaq_regular", first_fetch)
    first = collectors.collect_regular_window(
        mode="close",
        stage="close",
        trade_date="2026-08-14",
        start_et="15:45",
        end_et="16:00",
        store_root=tmp_path,
        watchlist=_watchlist(["AMD", "NVDA"]),
        completeness=_completeness(),
        access=_access(),
    )
    assert first["snapshot_written"] is True
    latest = json.loads(
        (tmp_path / "snapshots/2026-08/2026-08-14/close/latest.json").read_text()
    )
    first_snapshot = json.loads((tmp_path / latest["snapshot_path"]).read_text())
    assert len(first_snapshot["data_refs"]) == 2
    assert "NVDA" in first_snapshot["missing"]

    monkeypatch.setattr(
        collectors,
        "fetch_nasdaq_regular",
        lambda symbol, *_args, **_kwargs: [
            {
                "symbol": symbol,
                "asset_class": "stocks",
                "session": "regular",
                "event_time": "2026-08-14T15:59:30-04:00",
                "source_timestamp": "2026-08-14T15:59:30-04:00",
                "last_sale": 202.0,
                "reported_volume": 30.0,
            }
        ],
    )
    retry = collectors.collect_regular_window(
        mode="close_retry",
        stage="close",
        trade_date="2026-08-14",
        start_et="15:45",
        end_et="16:00",
        store_root=tmp_path,
        watchlist=_watchlist(["AMD", "NVDA"]),
        completeness=_completeness(),
        access=_access(),
        symbols_override=["NVDA"],
    )
    assert retry["snapshot_written"] is True
    latest = json.loads(
        (tmp_path / "snapshots/2026-08/2026-08-14/close/latest.json").read_text()
    )
    retry_snapshot = json.loads((tmp_path / latest["snapshot_path"]).read_text())
    assert len(retry_snapshot["data_refs"]) == 3
    assert retry_snapshot["missing"] == []
