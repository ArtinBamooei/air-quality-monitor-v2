import json
import sqlite3
from datetime import datetime, timezone

from src.analytics import days_in_year, european_aqi, normalize_utc, refresh_analytics


def test_european_aqi_uses_worst_pollutant_category():
    assert european_aqi({"pm2_5": 4.0, "pm10": 16.0}) == 2
    assert european_aqi({"pm2_5": 150.0}) == 6
    assert european_aqi({"co": 12}) is None
    assert normalize_utc("2026-01-01T01:00") == "2026-01-01T01:00:00+00:00"


def test_european_aqi_rejects_invalid_and_non_finite_values():
    import pytest

    with pytest.raises(ValueError):
        european_aqi({"pm2_5": float("nan")})
    with pytest.raises(ValueError):
        european_aqi({"pm10": -1})


def test_gregorian_leap_year_boundaries():
    assert days_in_year(2000) == 366
    assert days_in_year(1900) == 365
    assert days_in_year(2024) == 366


def test_open_meteo_crosscheck_indices_remain_index_values():
    from src import collector
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows, invalid, out_of_range, stale = collector.silver_rows(
        "open_meteo", {"current": {"european_aqi": 2, "us_aqi": 35}},
        now, now, "r1")
    assert invalid == out_of_range == stale == 0
    assert {(r[1], r[4]) for r in rows} == {("european_aqi", "index"), ("us_aqi", "index")}


def test_refresh_builds_clean_gold_and_marts(tmp_path):
    db = tmp_path / "analytics.db"
    config = tmp_path / "cities.json"
    config.write_text(json.dumps([{"name": "A", "country": "Iran", "lat": 1, "lon": 2}]))
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE silver_measurements(city TEXT,country TEXT,provider TEXT,pollutant TEXT,
            value REAL,unit TEXT,value_type TEXT,observed_at TEXT,ingested_at TEXT,run_id TEXT,quality_flag TEXT)""")
        conn.executemany("INSERT INTO silver_measurements VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            ("A", "Iran", "open_meteo", "pm2_5", 10, "µg/m³", "concentration", "2026-01-01T00:00:00+00:00", "x", "r1", "ok"),
            ("A", "Iran", "openweather", "pm2_5", 20, "µg/m³", "concentration", "2026-01-01T00:00:00+00:00", "x", "r1", "ok"),
            ("A", "Iran", "pm2_5", "pm2_5", 999, "µg/m³", "concentration", "2026-01-01T00:00:00+00:00", "x", "r1", "stale"),
            ("A", "Iran", "waqi", "pm2_5", 100, "AQI", "index", "2026-01-01T00:00:00+00:00", "x", "r1", "ok"),
        ])
        conn.execute("CREATE TABLE collection_runs(run_id TEXT PRIMARY KEY,started_at TEXT,finished_at TEXT,status TEXT,total_pairs INTEGER,failed_pairs INTEGER)")
        conn.execute("CREATE TABLE bronze_responses(run_id TEXT,provider TEXT,status TEXT)")
    refresh_analytics(db, config)
    with sqlite3.connect(db) as conn:
        fact = conn.execute("SELECT value,n_sources,spread FROM fact_air_quality").fetchone()
        assert fact == (15.0, 2, 10.0)
        assert conn.execute("SELECT COUNT(*) FROM provider_disagreement").fetchone()[0] == 1
        assert conn.execute("SELECT european_aqi FROM air_quality_indices").fetchone()[0] == 2
        assert conn.execute("SELECT n_records FROM daily_city_summary").fetchone()[0] == 1


def test_country_comparison_weights_hours_equally(tmp_path):
    db = tmp_path / "country.db"
    config = tmp_path / "cities.json"
    config.write_text(json.dumps([
        {"name": "A", "country": "Iran", "lat": 1, "lon": 2},
        {"name": "B", "country": "Iran", "lat": 3, "lon": 4},
    ]))
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE silver_measurements(city TEXT,country TEXT,provider TEXT,pollutant TEXT,
            value REAL,unit TEXT,value_type TEXT,observed_at TEXT,ingested_at TEXT,run_id TEXT,quality_flag TEXT)""")
        conn.executemany("INSERT INTO silver_measurements VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            ("A", "Iran", "open_meteo", "pm2_5", 0, "µg/m³", "concentration", "2026-01-01T00:00:00+00:00", "x", "r", "ok"),
            ("B", "Iran", "open_meteo", "pm2_5", 100, "µg/m³", "concentration", "2026-01-01T00:00:00+00:00", "x", "r", "ok"),
            ("A", "Iran", "open_meteo", "pm2_5", 0, "µg/m³", "concentration", "2026-01-01T01:00:00+00:00", "x", "r", "ok"),
        ])
        conn.execute("CREATE TABLE collection_runs(run_id TEXT PRIMARY KEY,started_at TEXT,finished_at TEXT,status TEXT,total_pairs INTEGER,failed_pairs INTEGER)")
        conn.execute("CREATE TABLE bronze_responses(run_id TEXT,provider TEXT,status TEXT)")
    refresh_analytics(db, config)
    with sqlite3.connect(db) as conn:
        # Equal weight for hour values 50 and 0; row-weighting would yield 33.3.
        assert conn.execute("SELECT mean FROM country_comparison").fetchone()[0] == 25
