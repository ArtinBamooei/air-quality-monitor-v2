import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src import backfill as history
from src import collector


def test_backfill_response_uses_bronze_silver_dq_and_stale_gold(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "DB_PATH", tmp_path / "backfill.db")
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    (tmp_path / "config").mkdir()
    city = {"name": "History City", "country": "Iran", "lat": 35.0, "lon": 51.0}
    (tmp_path / "config" / "cities.json").write_text(json.dumps([city]), encoding="utf-8")

    old = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    times = [old.isoformat(), (old + timedelta(hours=1)).isoformat()]
    payload = {
        "hourly": {"time": times, "pm2_5": [12.0, 13.0], "pm10": [20.0, 21.0],
                   "carbon_monoxide": [100.0, 101.0], "nitrogen_dioxide": [4.0, 4.1],
                   "sulphur_dioxide": [1.0, 1.1], "ozone": [30.0, 31.0]},
        "hourly_units": {"pm2_5": "µg/m³", "pm10": "µg/m³"},
    }
    response = collector.ApiResponse(payload, 200)
    samples = history._open_meteo_samples(payload)
    monkeypatch.setattr(history, "_fetch_open_meteo", lambda _city, _days: (response, samples))

    result = history.backfill(days=2)
    assert result["providers"] == ["open_meteo"]
    assert result["gold_rows"] == 2
    with collector.sqlite3.connect(collector.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM bronze_responses").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM silver_measurements").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM air_quality").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM air_quality WHERE quality_flag='stale'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM dq_results WHERE run_id=? AND check_name='freshness' AND action='mark_stale'",
                          (result["run_id"],)).fetchone()[0] == 1

    # Mark one observation as live; replaying history must neither duplicate it
    # nor downgrade that live status back to stale.
    with collector.sqlite3.connect(collector.DB_PATH) as db:
        live_time = db.execute("SELECT MIN(observed_at) FROM air_quality").fetchone()[0]
        db.execute("UPDATE air_quality SET quality_flag='ok' WHERE observed_at=?", (live_time,))
    history.backfill(days=2)
    with collector.sqlite3.connect(collector.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM silver_measurements").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM air_quality").fetchone()[0] == 2
        assert db.execute("SELECT quality_flag FROM air_quality WHERE observed_at=?", (live_time,)).fetchone()[0] == "ok"


def test_open_meteo_backfill_requests_documented_past_days(monkeypatch):
    city = {"name": "History City", "lat": 35.0, "lon": 51.0}
    payload = {"hourly": {"time": ["2026-09-29T10:00"], "pm2_5": [1], "pm10": [2],
                           "carbon_monoxide": [3], "nitrogen_dioxide": [4],
                           "sulphur_dioxide": [5], "ozone": [6]}}
    response = Mock(status_code=200)
    response.json.return_value = payload
    request = Mock(return_value=response)
    monkeypatch.setattr(collector.requests, "get", request)
    _, samples = history._fetch_open_meteo(city, 92)
    assert len(samples) == 1
    params = request.call_args.kwargs["params"]
    assert params["past_days"] == 92
    assert params["forecast_days"] == 0
    assert "hourly" in params


def test_openweather_history_is_mocked_and_parsed(monkeypatch):
    monkeypatch.setenv("OPENWEATHER_API_KEY", "fixture-key")
    item = {"dt": int(datetime.now(timezone.utc).timestamp()), "main": {"aqi": 2},
            "components": {"pm2_5": 8.0, "pm10": 12.0, "co": 10,
                           "no2": 1, "so2": 1, "o3": 20}}
    response = Mock(status_code=200)
    response.json.return_value = {"list": [item]}
    request = Mock(return_value=response)
    monkeypatch.setattr(collector.requests, "get", request)
    history_rows = list(history._fetch_openweather_chunks(
        {"name": "History City", "lat": 35.0, "lon": 51.0}, 1))
    assert len(history_rows) == 1
    assert history_rows[0][1][0][1]["pm2_5"] == 8.0
    assert request.call_args.args[0].endswith("/air_pollution/history")


def test_backfill_rejects_more_than_92_days():
    with pytest.raises(ValueError):
        history.backfill(days=93)
