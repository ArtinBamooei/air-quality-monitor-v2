from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src import collector


@pytest.fixture
def city():
    return {"name": "Test City", "country": "Iran", "lat": 35.7, "lon": 51.4}


def mock_http(monkeypatch, payload, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    monkeypatch.setattr(collector.requests, "get", Mock(return_value=response))
    return response


def test_open_meteo_sample_is_parsed_without_network(monkeypatch, city):
    sample = {"current": {"time": "2026-09-29T12:00", "pm2_5": 12.5, "pm10": 20,
                          "carbon_monoxide": 110, "nitrogen_dioxide": 4.2,
                          "sulphur_dioxide": 1.1, "ozone": 30},
              "current_units": {"pm2_5": "µg/m³", "pm10": "µg/m³"}}
    response = mock_http(monkeypatch, sample)
    result = collector.fetch_open_meteo(city)
    assert result["pm2_5"] == 12.5
    assert result["_raw"] == sample
    assert result["_http_status"] == 200
    response.json.assert_called_once_with()


def test_openweather_sample_is_parsed_without_network(monkeypatch, city):
    monkeypatch.setenv("OPENWEATHER_API_KEY", "test-key")
    observed_epoch = int(datetime.now(timezone.utc).timestamp())
    sample = {"list": [{"dt": observed_epoch, "main": {"aqi": 2},
                        "components": {"pm2_5": 8.1, "pm10": 12.0, "co": 240,
                                       "no2": 3.0, "so2": 1.0, "o3": 20.0}}]}
    mock_http(monkeypatch, sample)
    result = collector.fetch_openweather(city)
    assert result["pm2_5"] == 8.1
    assert result["_reported_aqi"] == 2
    assert result["_raw"] == sample


def test_waqi_sample_is_parsed_without_network(monkeypatch, city):
    monkeypatch.setenv("WAQI_API_TOKEN", "test-token")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sample = {"status": "ok", "data": {"aqi": 42, "time": {"iso": now},
              "city": {"name": "Test Station", "geo": [35.7, 51.4]},
              "iaqi": {"pm25": {"v": 18}, "pm10": {"v": 28}}}}
    mock_http(monkeypatch, sample)
    result = collector.fetch_waqi(city)
    assert result["aqi"] == 42
    assert result["pm2_5"] is None  # WAQI pollutant subindices are indices, not concentrations.
    assert result["_observed_at"] == now
    assert result["_raw"] == sample


def test_repeated_collection_upserts_silver_rows(monkeypatch, tmp_path, city):
    monkeypatch.setattr(collector, "DB_PATH", tmp_path / "test.db")
    collector.init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    raw = {"current": {"time": now, "pm2_5": 10, "pm10": 20,
                        "carbon_monoxide": 100, "nitrogen_dioxide": 4,
                        "sulphur_dioxide": 2, "ozone": 5},
           "current_units": {"pm2_5": "µg/m³", "pm10": "µg/m³"}}
    def provider(_):
        return {"aqi": None, "pm2_5": 10, "pm10": 20, "co": 100,
                "no2": 4, "so2": 2, "o3": 5, "_observed_at": now,
                "_raw": raw, "_http_status": 200}
    monkeypatch.setattr(collector, "PROVIDERS", (("open_meteo", provider),))

    collector.collect_city(city, run_id="repeatable-run")
    with collector.sqlite3.connect(collector.DB_PATH) as db:
        first_count = db.execute("SELECT COUNT(*) FROM silver_measurements").fetchone()[0]
    collector.collect_city(city, run_id="repeatable-run")
    with collector.sqlite3.connect(collector.DB_PATH) as db:
        second_count = db.execute("SELECT COUNT(*) FROM silver_measurements").fetchone()[0]
        duplicate_keys = db.execute("""SELECT COUNT(*) FROM (
            SELECT city,provider,pollutant,COALESCE(observed_at,''),COUNT(*) AS n
            FROM silver_measurements GROUP BY 1,2,3,4 HAVING n>1)""").fetchone()[0]
    assert first_count == second_count == 6
    assert duplicate_keys == 0


def test_silver_upsert_preserves_prior_run_lineage(monkeypatch, tmp_path, city):
    monkeypatch.setattr(collector, "DB_PATH", tmp_path / "lineage.db")
    collector.init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    raw = {"current": {"time": now, "pm2_5": 10},
           "current_units": {"pm2_5": "µg/m³"}}

    def provider(_city):
        return {"aqi": None, "pm2_5": 10, "pm10": None, "co": None, "no2": None,
                "so2": None, "o3": None, "_observed_at": now,
                "_raw": raw, "_http_status": 200}

    monkeypatch.setattr(collector, "PROVIDERS", (("open_meteo", provider),))
    collector.collect_city(city, run_id="run-a")
    collector.collect_city(city, run_id="run-b")
    with collector.sqlite3.connect(collector.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM silver_measurements").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM silver_measurement_lineage").fetchone()[0] == 2


def test_silver_rejects_missing_observation_timestamp():
    rows, invalid, _, stale = collector.silver_rows(
        "open_meteo", {"current": {"pm2_5": 12}}, None,
        datetime.now(timezone.utc).isoformat(), "r1")
    assert rows == []
    assert invalid == 1
    assert stale == 0
