"""Rebuild dimensional Gold facts, AQI indices, and analytical marts from Silver."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from math import ceil, isfinite
from numbers import Real
from pathlib import Path
from statistics import median

from src.aqi import calculate_pm25_aqi
from src import quality
from src.database import connect
from src.settings import SETTINGS

ROOT = Path(__file__).resolve().parents[1]
EEA_BANDS = {
    "pm2_5": (5, 15, 50, 90, 140), "pm10": (15, 45, 120, 195, 270),
    "o3": (60, 100, 120, 160, 180), "no2": (10, 25, 60, 100, 150),
    "so2": (20, 40, 125, 190, 275),
}
WHO_24H_UG_M3 = {"pm2_5": 15, "pm10": 45, "no2": 25, "so2": 40,
                 "co": 4_000}  # WHO CO guideline is 4 mg/m³ = 4,000 µg/m³.


def european_aqi(concentrations: dict[str, float]) -> int | None:
    """Return EEA hourly category 1 (good) through 6 (extremely poor).

    Unknown pollutants are ignored; invalid concentrations are rejected instead
    of silently becoming an apparently valid category.
    """
    categories = []
    for pollutant, value in concentrations.items():
        bands = EEA_BANDS.get(pollutant)
        if bands is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, Real)
                or not isfinite(float(value)) or float(value) < 0):
            raise ValueError(f"invalid concentration for {pollutant}")
        categories.append(next((i + 1 for i, edge in enumerate(bands) if value <= edge), 6))
    return max(categories) if categories else None


def normalize_utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return parsed.replace(microsecond=0).isoformat()


def normalize_utc_hour(value: str) -> str:
    return datetime.fromisoformat(normalize_utc(value)).replace(minute=0, second=0).isoformat()


def days_in_year(year: int) -> int:
    """Return the number of days using the Gregorian leap-year rule."""
    return 366 if year % 400 == 0 or (year % 4 == 0 and year % 100 != 0) else 365


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS dim_city(city_key INTEGER PRIMARY KEY, city TEXT UNIQUE, country TEXT, latitude REAL, longitude REAL);
    CREATE TABLE IF NOT EXISTS dim_pollutant(pollutant_key INTEGER PRIMARY KEY, pollutant TEXT UNIQUE, canonical_unit TEXT);
    CREATE TABLE IF NOT EXISTS dim_provider(provider_key INTEGER PRIMARY KEY, provider TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS dim_time(time_key TEXT PRIMARY KEY, date TEXT, hour INTEGER, year INTEGER, month INTEGER, day INTEGER);
    CREATE TABLE IF NOT EXISTS fact_air_quality(city_key INTEGER NOT NULL REFERENCES dim_city(city_key),
      pollutant_key INTEGER NOT NULL REFERENCES dim_pollutant(pollutant_key),
      time_key TEXT NOT NULL REFERENCES dim_time(time_key), value REAL NOT NULL, unit TEXT NOT NULL,
      n_sources INTEGER NOT NULL, spread REAL, agreement_flag TEXT NOT NULL, run_id TEXT,
      PRIMARY KEY(city_key,pollutant_key,time_key));
    CREATE TABLE IF NOT EXISTS fact_air_quality_sources(city_key INTEGER NOT NULL REFERENCES dim_city(city_key),
      pollutant_key INTEGER NOT NULL REFERENCES dim_pollutant(pollutant_key),
      time_key TEXT NOT NULL REFERENCES dim_time(time_key),
      provider_key INTEGER NOT NULL REFERENCES dim_provider(provider_key), value REAL NOT NULL,
      unit TEXT NOT NULL, run_id TEXT NOT NULL,
      PRIMARY KEY(city_key,pollutant_key,time_key,provider_key));
    CREATE TABLE IF NOT EXISTS historical_fact_air_quality(city TEXT, country TEXT, pollutant TEXT,
      observed_at TEXT, value REAL, unit TEXT, n_sources INTEGER, quality_flag TEXT,
      PRIMARY KEY(city,pollutant,observed_at));
    CREATE TABLE IF NOT EXISTS provider_disagreement(city TEXT, pollutant TEXT, observed_at TEXT,
      n_sources INTEGER, min_value REAL, max_value REAL, absolute_spread REAL, relative_spread REAL,
      agreement_flag TEXT, PRIMARY KEY(city,pollutant,observed_at));
    CREATE TABLE IF NOT EXISTS air_quality_indices(city TEXT, country TEXT, observed_at TEXT,
      european_aqi INTEGER, epa_pm25_aqi INTEGER, open_meteo_european_aqi REAL,
      open_meteo_us_aqi REAL, index_label TEXT, run_id TEXT, quality_flag TEXT DEFAULT 'ok',
      PRIMARY KEY(city,observed_at));
    CREATE TABLE IF NOT EXISTS daily_city_summary(city TEXT, country TEXT, pollutant TEXT, date TEXT,
      mean REAL, maximum REAL, p95 REAL, rolling_7d_mean REAL, n_records INTEGER,
      rolling_7d_days INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(city,pollutant,date));
    CREATE TABLE IF NOT EXISTS country_comparison(country TEXT, pollutant TEXT, date TEXT,
      mean REAL, n_cities INTEGER, city_list TEXT, n_records INTEGER, methodology TEXT,
      PRIMARY KEY(country,pollutant,date));
    CREATE TABLE IF NOT EXISTS who_exceedance_days(city TEXT, country TEXT, pollutant TEXT, year INTEGER,
      threshold REAL, days_over INTEGER, eligible_days INTEGER, coverage REAL,
      PRIMARY KEY(city,pollutant,year));
    CREATE TABLE IF NOT EXISTS weather_hourly(city TEXT, observed_at TEXT, temperature_c REAL,
      precipitation_mm REAL, wind_speed_kmh REAL, run_id TEXT,
      PRIMARY KEY(city,observed_at));
    CREATE TABLE IF NOT EXISTS weather_air_quality_join(city TEXT, observed_at TEXT, pollutant TEXT,
      concentration REAL, temperature_c REAL, precipitation_mm REAL, wind_speed_kmh REAL,
      PRIMARY KEY(city,observed_at,pollutant));
    """)
    fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(fact_air_quality)")}
    if not {"city_key", "pollutant_key", "time_key"}.issubset(fact_cols):
        # This is a derived table and can be safely rebuilt from Silver.
        conn.execute("DROP TABLE fact_air_quality")
        conn.execute("""CREATE TABLE fact_air_quality(city_key INTEGER NOT NULL REFERENCES dim_city(city_key),
          pollutant_key INTEGER NOT NULL REFERENCES dim_pollutant(pollutant_key),
          time_key TEXT NOT NULL REFERENCES dim_time(time_key), value REAL NOT NULL, unit TEXT NOT NULL,
          n_sources INTEGER NOT NULL, spread REAL, agreement_flag TEXT NOT NULL, run_id TEXT,
          PRIMARY KEY(city_key,pollutant_key,time_key))""")
    index_cols = {r[1] for r in conn.execute("PRAGMA table_info(air_quality_indices)")}
    for name in ("open_meteo_european_aqi", "open_meteo_us_aqi"):
        if name not in index_cols:
            conn.execute(f"ALTER TABLE air_quality_indices ADD COLUMN {name} REAL")
    if "quality_flag" not in index_cols:
        conn.execute("ALTER TABLE air_quality_indices ADD COLUMN quality_flag TEXT NOT NULL DEFAULT 'ok'")
    daily_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_city_summary)")}
    if "rolling_7d_days" not in daily_cols:
        conn.execute("ALTER TABLE daily_city_summary ADD COLUMN rolling_7d_days INTEGER NOT NULL DEFAULT 0")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(collection_runs)")}
    for name, kind in (("bronze_rows", "INTEGER NOT NULL DEFAULT 0"),
                       ("silver_rows", "INTEGER NOT NULL DEFAULT 0"),
                       ("gold_rows", "INTEGER NOT NULL DEFAULT 0"),
                       ("provider_errors", "TEXT NOT NULL DEFAULT '{}'")):
        if name not in cols:
            conn.execute(f"ALTER TABLE collection_runs ADD COLUMN {name} {kind}")
    pipeline_kind = conn.execute("SELECT type FROM sqlite_master WHERE name='pipeline_runs'").fetchone()
    if pipeline_kind and pipeline_kind[0] == "view":
        conn.execute("DROP VIEW pipeline_runs")
    conn.execute("""CREATE TABLE IF NOT EXISTS pipeline_runs(
        run_id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
        total_pairs INTEGER NOT NULL,failed_pairs INTEGER NOT NULL,bronze_rows INTEGER NOT NULL,
        silver_rows INTEGER NOT NULL,gold_rows INTEGER NOT NULL,provider_errors TEXT NOT NULL)""")


def refresh_analytics(db_path: str | Path, city_config: str | Path | None = None) -> None:
    """Idempotently rebuild derived Gold tables and marts from clean Silver facts."""
    cities = json.loads(Path(city_config or ROOT / "config" / "cities.json").read_text(encoding="utf-8"))
    with connect(db_path, foreign_keys=True) as conn:
        conn.row_factory = sqlite3.Row
        _create_schema(conn)
        for table in ("fact_air_quality", "fact_air_quality_sources", "historical_fact_air_quality",
                      "provider_disagreement", "air_quality_indices", "daily_city_summary",
                      "country_comparison", "who_exceedance_days", "weather_air_quality_join",
                      "dim_time", "dim_provider", "dim_pollutant", "dim_city"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany("INSERT INTO dim_city(city,country,latitude,longitude) VALUES(?,?,?,?)",
                         [(c["name"], c["country"], c["lat"], c["lon"]) for c in cities])
        silver_all = conn.execute("""SELECT city,country,provider,pollutant,value,unit,value_type,
            observed_at,ingested_at,run_id,quality_flag FROM silver_measurements
            WHERE quality_flag IN ('ok','stale') AND value_type='concentration'
            AND observed_at IS NOT NULL ORDER BY observed_at""").fetchall()
        silver = [row for row in silver_all if row["quality_flag"] == "ok"]
        pollutants = sorted({r["pollutant"] for r in silver_all})
        providers = sorted({r["provider"] for r in silver_all})
        conn.executemany("INSERT INTO dim_pollutant(pollutant,canonical_unit) VALUES(?,?)",
                         [(p, "µg/m³") for p in pollutants])
        conn.executemany("INSERT INTO dim_provider(provider) VALUES(?)", [(p,) for p in providers])
        city_keys = {r["city"]: r["city_key"] for r in conn.execute("SELECT city_key,city FROM dim_city")}
        pollutant_keys = {r["pollutant"]: r["pollutant_key"] for r in conn.execute("SELECT pollutant_key,pollutant FROM dim_pollutant")}
        provider_keys = {r["provider"]: r["provider_key"] for r in conn.execute("SELECT provider_key,provider FROM dim_provider")}
        times = sorted({normalize_utc_hour(r["observed_at"]) for r in silver_all})
        conn.executemany("INSERT INTO dim_time VALUES(?,?,?,?,?,?)", [(t,t[:10],int(t[11:13]),int(t[:4]),int(t[5:7]),int(t[8:10])) for t in times])
        grouped = defaultdict(list)
        for row in silver:
            grouped[(row["city"], row["country"], row["pollutant"], normalize_utc_hour(row["observed_at"]))].append(row)
        facts = []
        for (city, country, pollutant, stamp), records in grouped.items():
            by_provider = defaultdict(list)
            for record in records:
                by_provider[record["provider"]].append(record)
            provider_values = {provider: median(float(r["value"]) for r in rows)
                               for provider, rows in by_provider.items()}
            vals = list(provider_values.values())
            center = median(vals)
            spread = max(vals) - min(vals)
            relative = spread / abs(center) if center else (0.0 if spread == 0 else None)
            _, _, agreement = quality.check_agreement(vals)
            flag = agreement if len(vals) > 1 else "single_source"
            latest_record = max(records, key=lambda row: row["ingested_at"] or "")
            facts.append((city_keys[city],pollutant_keys[pollutant],stamp,center,
                          records[0]["unit"],len(vals),spread,flag,latest_record["run_id"]))
            conn.executemany("""INSERT INTO fact_air_quality_sources
                (city_key,pollutant_key,time_key,provider_key,value,unit,run_id) VALUES(?,?,?,?,?,?,?)""",
                [(city_keys[city],pollutant_keys[pollutant],stamp,provider_keys[provider],value,
                  max(by_provider[provider],key=lambda row: row["ingested_at"] or "")["unit"],
                  max(by_provider[provider],key=lambda row: row["ingested_at"] or "")["run_id"])
                 for provider,value in provider_values.items()])
            conn.execute("INSERT INTO provider_disagreement VALUES(?,?,?,?,?,?,?,?,?)",
                         (city,pollutant,stamp,len(vals),min(vals),max(vals),spread,relative,flag))
        conn.executemany("INSERT INTO fact_air_quality VALUES(?,?,?,?,?,?,?,?,?)", facts)
        # Historical analytics normalize observations to UTC hours first. This
        # prevents sub-hour polling from falsely inflating daily sample counts.
        hourly = defaultdict(list)
        for row in silver_all:
            hour = normalize_utc_hour(row["observed_at"])
            hourly[(row["city"], row["country"], row["pollutant"], hour)].append(row)
        history_facts = []
        history_values = {}
        history_by_city_day = defaultdict(list)
        history_index_data = defaultdict(dict)
        history_index_flags = defaultdict(list)
        for (city, country, pollutant, stamp), rows in hourly.items():
            # Prefer fresh observations when a backfill and a live collection
            # overlap in the same UTC hour. If none are fresh, retain the stale
            # history without allowing it to masquerade as current data.
            fresh_rows = [row for row in rows if row["quality_flag"] == "ok"]
            effective_rows = fresh_rows or rows
            by_provider = defaultdict(list)
            for row in effective_rows:
                by_provider[row["provider"]].append(float(row["value"]))
            value = median(median(values) for values in by_provider.values())
            quality_flag = "ok" if fresh_rows else "stale"
            history_facts.append((city,country,pollutant,stamp,value,rows[0]["unit"],
                                  len({row["provider"] for row in effective_rows}),quality_flag))
            history_values[(city,country,pollutant,stamp)] = value
            history_by_city_day[(city,country,pollutant,stamp[:10])].append((stamp,value))
            if pollutant in EEA_BANDS:
                history_index_data[(city,country,stamp)][pollutant] = value
                history_index_flags[(city,country,stamp)].append(quality_flag)
        conn.executemany("INSERT INTO historical_fact_air_quality VALUES(?,?,?,?,?,?,?,?)", history_facts)
        crosscheck_by_hour = {(r["city"],normalize_utc_hour(r["observed_at"]),r["pollutant"]):r["value"] for r in conn.execute(
            """SELECT city,observed_at,pollutant,value FROM silver_measurements
            WHERE provider='open_meteo' AND quality_flag IN ('ok','stale') AND value_type='index'
            AND pollutant IN ('european_aqi','us_aqi')""")}
        idx_rows = []
        for (city,country,stamp), vals in history_index_data.items():
            pm = history_values.get((city,country,"pm2_5",stamp))
            eaqi = european_aqi(vals)
            ep = calculate_pm25_aqi(pm) if pm is not None else None
            idx_rows.append((city,country,stamp,eaqi,ep,crosscheck_by_hour.get((city,stamp,"european_aqi")),
                             crosscheck_by_hour.get((city,stamp,"us_aqi")),"hourly modeled proxy; not regulatory AQI",None,
                             "ok" if all(flag == "ok" for flag in history_index_flags[(city,country,stamp)]) else "stale"))
        conn.executemany("""INSERT INTO air_quality_indices(city,country,observed_at,european_aqi,
            epa_pm25_aqi,open_meteo_european_aqi,open_meteo_us_aqi,index_label,run_id,quality_flag)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", idx_rows)
        dates_by_key = defaultdict(dict)
        for (city,country,pollutant,date), observations in history_by_city_day.items():
            vals = [value for _, value in sorted(observations)]
            vals.sort()
            dates_by_key[(city,pollutant)][date] = (country, vals)
        daily = []
        daily_means = defaultdict(list)
        for (city,pollutant), dated in dates_by_key.items():
            for date in sorted(dated):
                country, vals = dated[date]
                daily_means[(city,pollutant)].append((date, sum(vals)/len(vals)))
                hist = [(d,v) for d,v in daily_means[(city,pollutant)]
                        if 0 <= (datetime.fromisoformat(date)-datetime.fromisoformat(d)).days < 7]
                p95 = vals[max(0, ceil(.95 * len(vals)) - 1)]
                daily.append((city,country,pollutant,date,sum(vals)/len(vals),max(vals),p95,
                              sum(v for _,v in hist)/len(hist),len(vals),len(hist)))
        conn.executemany("""INSERT INTO daily_city_summary
            (city,country,pollutant,date,mean,maximum,p95,rolling_7d_mean,n_records,rolling_7d_days)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", daily)
        country_hour_values = defaultdict(list)
        for (city,country,pollutant,date), observations in history_by_city_day.items():
            for stamp, value in observations:
                country_hour_values[(country,pollutant,date,stamp)].append((city,value))
        country_daily = defaultdict(list)
        country_city_counts = defaultdict(set)
        for (country,pollutant,date,stamp), city_values in country_hour_values.items():
            country_daily[(country,pollutant,date)].append(median(value for _,value in city_values))
            country_city_counts[(country,pollutant,date)].update(city for city,_ in city_values)
        conn.executemany("INSERT INTO country_comparison VALUES(?,?,?,?,?,?,?,?)", [
            (country,pollutant,date,sum(hour_values)/len(hour_values),
             len(country_city_counts[(country,pollutant,date)]),
             json.dumps(sorted(country_city_counts[(country,pollutant,date)])),len(hour_values),
             "UTC hours equally weighted; each hour is the median across represented cities; historical stale retained")
            for (country,pollutant,date),hour_values in sorted(country_daily.items())])
        daily_averages = defaultdict(list)
        for (city,country,pollutant,date), observations in history_by_city_day.items():
            if pollutant in WHO_24H_UG_M3:
                hourly_values = [v for _, v in observations]
                # Values are unique UTC hours; require at least 18 distinct hours.
                if len(hourly_values) >= 18:
                    daily_averages[(city,country,pollutant,date[:4])].append(sum(hourly_values)/len(hourly_values))
        ex = defaultdict(list)
        for (city,country,pollutant,year), vals in daily_averages.items():
            ex[(city,country,pollutant,year)].append((len(vals),sum(v > WHO_24H_UG_M3[pollutant] for v in vals)))
        conn.executemany("INSERT INTO who_exceedance_days VALUES(?,?,?,?,?,?,?,?)", [
            (city,country,pollutant,int(year),WHO_24H_UG_M3[pollutant],over,eligible,
             eligible/days_in_year(int(year)))
            for (city,country,pollutant,year), vals in ex.items()
            for eligible,over in [(sum(x[0] for x in vals),sum(x[1] for x in vals))]])
        conn.execute("""INSERT INTO weather_air_quality_join(city,observed_at,pollutant,concentration,
            temperature_c,precipitation_mm,wind_speed_kmh) SELECT f.city,f.observed_at,f.pollutant,f.value,
            w.temperature_c,w.precipitation_mm,w.wind_speed_kmh FROM historical_fact_air_quality f JOIN weather_hourly w
            ON w.city=f.city AND w.observed_at=f.observed_at""")
        # Record true layer counts for completed runs; errors remain provider granular.
        run_ids = [r[0] for r in conn.execute("SELECT run_id FROM collection_runs WHERE finished_at IS NOT NULL")]
        for run_id in run_ids:
            conn.execute("""UPDATE collection_runs SET
                bronze_rows=(SELECT COUNT(*) FROM bronze_responses WHERE run_id=?),
                silver_rows=(SELECT COUNT(*) FROM silver_measurements WHERE run_id=?),
                gold_rows=(SELECT COUNT(*) FROM fact_air_quality WHERE run_id=?) WHERE run_id=?""",
                (run_id, run_id, run_id, run_id))
        conn.execute("""INSERT INTO pipeline_runs(run_id,started_at,finished_at,status,total_pairs,
            failed_pairs,bronze_rows,silver_rows,gold_rows,provider_errors)
            SELECT run_id,started_at,finished_at,
              CASE status WHEN 'complete' THEN 'ok' ELSE status END,total_pairs,failed_pairs,
              bronze_rows,silver_rows,gold_rows,provider_errors FROM collection_runs
            WHERE finished_at IS NOT NULL
            ON CONFLICT(run_id) DO UPDATE SET started_at=excluded.started_at,
              finished_at=excluded.finished_at,status=excluded.status,total_pairs=excluded.total_pairs,
              failed_pairs=excluded.failed_pairs,bronze_rows=excluded.bronze_rows,
              silver_rows=excluded.silver_rows,gold_rows=excluded.gold_rows,
              provider_errors=excluded.provider_errors""")


def fetch_weather_archive(city: dict, start_date: str, end_date: str, run_id: str) -> int:
    """Optional historical weather context from Open-Meteo reanalysis."""
    import requests
    response = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
        "latitude": city["lat"], "longitude": city["lon"], "start_date": start_date,
        "end_date": end_date, "hourly": "temperature_2m,precipitation,wind_speed_10m",
        "timezone": "UTC", "wind_speed_unit": "kmh"}, timeout=20)
    response.raise_for_status()
    data = response.json().get("hourly", {})
    rows = []
    for i, stamp in enumerate(data.get("time", [])):
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        stamp_utc = (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)).isoformat(timespec="seconds")
        rows.append((city["name"], stamp_utc, at(data,"temperature_2m",i),
                     at(data,"precipitation",i), at(data,"wind_speed_10m",i), run_id))
    with connect(SETTINGS.database_path) as conn:
        _create_schema(conn)
        conn.executemany("""INSERT INTO weather_hourly VALUES(?,?,?,?,?,?)
            ON CONFLICT(city,observed_at) DO UPDATE SET temperature_c=excluded.temperature_c,
            precipitation_mm=excluded.precipitation_mm,wind_speed_kmh=excluded.wind_speed_kmh,
            run_id=excluded.run_id""", rows)
    return len(rows)


def at(data: dict, key: str, index: int):
    values = data.get(key, [])
    return values[index] if index < len(values) else None
