"""Three-provider air-quality collection, validation, robust fusion and SQLite storage."""
from __future__ import annotations

import json
import hashlib
import logging
from contextlib import closing
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from math import radians, sin, cos, asin, sqrt
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from uuid import uuid4

import requests
from src import quality
from src.database import connect, enable_wal
from src.settings import SETTINGS

ROOT = Path(__file__).resolve().parents[1]

DB_PATH = SETTINGS.database_path
FIELDS = ("aqi", "pm2_5", "pm10", "co", "no2", "so2", "o3")
TIMEOUT = SETTINGS.request_timeout_seconds
CONCENTRATION_UNIT = "µg/m³"
logger = logging.getLogger("air_quality")


@dataclass(frozen=True)
class ApiResponse:
    payload: dict[str, Any]
    http_status: int


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None,
                 http_status: int | None = None):
        super().__init__(message)
        self.payload = payload
        self.http_status = http_status

def init_db() -> None:
    with closing(connect(DB_PATH)) as conn, conn:
        enable_wal(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS raw_observations (
            id INTEGER PRIMARY KEY, city_name TEXT NOT NULL, country TEXT NOT NULL,
            provider TEXT NOT NULL, fetched_at TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT, observed_at TEXT, ingested_at TEXT,
            station_name TEXT,
            station_distance_km REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bronze_responses (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, provider TEXT NOT NULL,
            city TEXT NOT NULL, country TEXT NOT NULL, ingested_at TEXT NOT NULL,
            http_status INTEGER, payload_json TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT, error_class TEXT)""")
        bronze_columns = {row[1] for row in conn.execute("PRAGMA table_info(bronze_responses)")}
        if "error_class" not in bronze_columns:
            conn.execute("ALTER TABLE bronze_responses ADD COLUMN error_class TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_bronze_run_city ON bronze_responses(run_id,city,provider)")
        conn.execute("""CREATE TABLE IF NOT EXISTS silver_measurements (
            id INTEGER PRIMARY KEY, city TEXT NOT NULL, country TEXT NOT NULL,
            provider TEXT NOT NULL, pollutant TEXT NOT NULL, value REAL NOT NULL,
            unit TEXT NOT NULL, value_type TEXT NOT NULL CHECK(value_type IN ('concentration','index')),
            observed_at TEXT, ingested_at TEXT NOT NULL, run_id TEXT NOT NULL,
            quality_flag TEXT NOT NULL CHECK(quality_flag IN ('ok','stale')),
            bronze_id INTEGER)""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_silver_city_pollutant_time ON silver_measurements(city,pollutant,observed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_silver_run ON silver_measurements(run_id,provider)")
        conn.execute("""CREATE TABLE IF NOT EXISTS silver_measurement_lineage (
            city TEXT NOT NULL, provider TEXT NOT NULL, pollutant TEXT NOT NULL,
            observed_at TEXT NOT NULL, run_id TEXT NOT NULL, ingested_at TEXT NOT NULL,
            bronze_id INTEGER, PRIMARY KEY(city,provider,pollutant,observed_at,run_id))""")
        # Collapse duplicates written by an older v2 build before enforcing
        # the observation key. COALESCE makes missing timestamps idempotent too.
        conn.execute("""DELETE FROM silver_measurements WHERE id NOT IN (
            SELECT MAX(id) FROM silver_measurements
            GROUP BY city,provider,pollutant,COALESCE(observed_at,''))""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_silver_observation
            ON silver_measurements(city,provider,pollutant,COALESCE(observed_at,''))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS dq_results (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, check_name TEXT NOT NULL,
            scope TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL,
            details TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, total_pairs INTEGER NOT NULL DEFAULT 0,
            failed_pairs INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS air_quality (
            id INTEGER PRIMARY KEY, city_name TEXT NOT NULL, country TEXT NOT NULL,
            timestamp TEXT NOT NULL, aqi REAL, pm2_5 REAL, pm10 REAL, co REAL,
            no2 REAL, so2 REAL, o3 REAL, source_count INTEGER NOT NULL,
            sources TEXT NOT NULL, quality_score REAL NOT NULL, run_id TEXT, quality_flag TEXT,
            UNIQUE(city_name, timestamp))""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_air_city_time ON air_quality(city_name,timestamp)")
        raw_columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_observations)")}
        for column, sql_type in (("observed_at", "TEXT"), ("station_name", "TEXT"),
                                 ("station_distance_km", "REAL"), ("ingested_at", "TEXT")):
            if column not in raw_columns:
                conn.execute(f"ALTER TABLE raw_observations ADD COLUMN {column} {sql_type}")
        conn.execute("UPDATE raw_observations SET ingested_at=COALESCE(ingested_at,fetched_at) WHERE ingested_at IS NULL")
        # Idempotency key: identical cleaned values for a city are stored once
        # per five-minute window, so retries do not inflate the database while
        # later scheduled readings still build a useful history.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(air_quality)")}
        if "data_hash" not in existing:
            conn.execute("ALTER TABLE air_quality ADD COLUMN data_hash TEXT")
        if "time_bucket" not in existing:
            conn.execute("ALTER TABLE air_quality ADD COLUMN time_bucket INTEGER")
        for column, sql_type in (("collected_at", "TEXT"), ("observed_at", "TEXT"),
                                 ("ingested_at", "TEXT"), ("source_times", "TEXT"),
                                 ("field_coverage", "REAL"), ("source_coverage", "REAL"),
                                 ("spread", "TEXT"), ("agreement_flag", "TEXT"),
                                 ("run_id", "TEXT"), ("quality_flag", "TEXT"),
                                 ("conflicts", "TEXT")):
            if column not in existing:
                conn.execute(f"ALTER TABLE air_quality ADD COLUMN {column} {sql_type}")
        # Backfill older databases, which predate these columns, and collapse
        # any matching legacy rows before adding the unique index.
        rows = conn.execute("SELECT id,city_name,timestamp,data_hash,time_bucket,quality_score,source_count,collected_at,observed_at,ingested_at,source_times,field_coverage,source_coverage,conflicts," + ",".join(FIELDS) + " FROM air_quality ORDER BY id").fetchall()
        seen = set()
        for row in rows:
            (row_id, city_name, stamp, digest, bucket, old_quality, source_count,
             collected_at, observed_at, ingested_at, source_times, field_coverage,
             source_coverage, conflicts, *values) = row
            if not digest:
                payload = json.dumps(dict(zip(FIELDS, values)), sort_keys=True, separators=(",", ":"), allow_nan=False)
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if bucket is None:
                try:
                    bucket = int(datetime.fromisoformat(stamp).timestamp()) // 300
                except (TypeError, ValueError):
                    bucket = 0
            key = (city_name, digest, bucket)
            if key in seen:
                conn.execute("DELETE FROM air_quality WHERE id=?", (row_id,))
            else:
                seen.add(key)
                populated = sum(value is not None for value in values)
                conn.execute("""UPDATE air_quality SET data_hash=?,time_bucket=?,
                    collected_at=COALESCE(collected_at,timestamp), source_times=COALESCE(source_times,'{}'),
                    observed_at=COALESCE(observed_at,timestamp), ingested_at=COALESCE(ingested_at,collected_at,timestamp),
                    field_coverage=COALESCE(field_coverage,?), source_coverage=COALESCE(source_coverage,?),
                    conflicts=COALESCE(conflicts,'[]'), quality_score=? WHERE id=?""",
                    (digest, bucket, populated / len(FIELDS) * 100,
                     old_quality if old_quality is not None else source_count / len(PROVIDERS) * 100,
                     populated / len(FIELDS) * 100, row_id))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_air_quality_dedup ON air_quality(city_name,data_hash,time_bucket)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_air_city_observed ON air_quality(city_name,observed_at)")
        from src.analytics import _create_schema
        _create_schema(conn)


def _get(url: str, params: dict[str, Any]) -> ApiResponse:
    response = None
    for attempt in range(SETTINGS.retry_attempts):
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": "AirQualityMonitorV2/1.0"})
            if response.status_code != 429 and not 500 <= response.status_code <= 599:
                break
            if attempt == SETTINGS.retry_attempts - 1:
                break
        except requests.Timeout as exc:
            if attempt == SETTINGS.retry_attempts - 1:
                raise ProviderError(f"Request failed ({type(exc).__name__})") from None
        except requests.RequestException as exc:
            raise ProviderError(f"Request failed ({type(exc).__name__})") from None
        time.sleep(0.5 * (2 ** attempt))
    assert response is not None
    try:
        payload = response.json()
    except ValueError:
        payload = {"_raw_text": response.text}
    if response.status_code >= 400:
        raise ProviderError(f"HTTP request failed (HTTP {response.status_code})",
                             payload=payload, http_status=response.status_code)
    return ApiResponse(payload, response.status_code)


def fetch_open_meteo(city: dict) -> dict:
    # Open-Meteo uses full pollutant names for gases; the internal schema keeps
    # the common short labels used by the other providers.
    api_fields = {"pm2_5": "pm2_5", "pm10": "pm10", "co": "carbon_monoxide",
                  "no2": "nitrogen_dioxide", "so2": "sulphur_dioxide", "o3": "ozone"}
    response = _get("https://air-quality-api.open-meteo.com/v1/air-quality", {
        "latitude": city["lat"], "longitude": city["lon"],
        "current": ",".join((*api_fields.values(), "european_aqi", "us_aqi")),
        "timezone": "UTC",
    })
    data = response.payload
    current = data.get("current", {})
    if not current:
        raise ProviderError("Open-Meteo returned no current measurements", payload=data, http_status=response.http_status)
    vals = {key: current.get(api_name) for key, api_name in api_fields.items()}
    vals["aqi"] = None
    vals["_observed_at"] = current.get("time")
    vals["_raw"] = data
    vals["_http_status"] = response.http_status
    return vals


def fetch_openweather(city: dict) -> dict:
    key = os.getenv("OPENWEATHER_API_KEY")
    if not key:
        raise RuntimeError("OPENWEATHER_API_KEY is not set")
    response = _get("https://api.openweathermap.org/data/2.5/air_pollution", {
        "lat": city["lat"], "lon": city["lon"], "appid": key,
    })
    data = response.payload
    if not data.get("list"):
        raise ProviderError("OpenWeather returned no measurements", payload=data, http_status=response.http_status)
    item = data["list"][0]
    components = item.get("components", {})
    # OpenWeather reports gas concentrations in µg/m³ and PM in µg/m³.
    return {"aqi": None, "pm2_5": components.get("pm2_5"), "pm10": components.get("pm10"),
            "co": components.get("co"), "no2": components.get("no2"),
            "so2": components.get("so2"), "o3": components.get("o3"),
            "_observed_at": datetime.fromtimestamp(item["dt"], timezone.utc).isoformat() if item.get("dt") else None,
            "_raw": data, "_http_status": response.http_status, "_reported_aqi": item.get("main", {}).get("aqi")}


def waqi_observation_time(time_info: dict) -> str | None:
    """Normalize WAQI data.time.iso (preferred) or local data.time.s to UTC."""
    value = time_info.get("iso") or time_info.get("s")
    if not value:
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            observed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if observed.tzinfo is None:
        zone = time_info.get("tz")
        if not zone:
            # A local wall-clock time without its timezone is not safe to label UTC.
            return None
        try:
            tzinfo = datetime.strptime(zone, "%z").tzinfo if re.fullmatch(r"[+-]\d{2}:?\d{2}", zone) else ZoneInfo(zone)
        except (ValueError, KeyError):
            return None
        observed = observed.replace(tzinfo=tzinfo)
    return observed.astimezone(timezone.utc).isoformat()


def fetch_waqi(city: dict) -> dict:
    token = os.getenv("WAQI_API_TOKEN")
    if not token:
        raise RuntimeError("WAQI_API_TOKEN is not set")
    response = _get(f"https://api.waqi.info/feed/geo:{city['lat']};{city['lon']}/", {"token": token})
    data = response.payload
    if data.get("status") != "ok":
        raise ProviderError("WAQI returned non-ok status", payload=data, http_status=response.http_status)
    payload = data.get("data", {})
    station = payload.get("city", {})
    station_geo = station.get("geo", [])
    distance_km = None
    if len(station_geo) >= 2:
        try:
            lat1, lon1, lat2, lon2 = map(radians, (city["lat"], city["lon"], float(station_geo[0]), float(station_geo[1])))
            dlat, dlon = lat2 - lat1, lon2 - lon1
            a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
            distance_km = 6371 * 2 * asin(sqrt(a))
        except (TypeError, ValueError):
            pass
    station_time = payload.get("time", {})
    observed_at = waqi_observation_time(station_time)
    # WAQI iaqi entries are pollutant-specific AQI indices, not concentrations.
    # Keep their full representation in the raw payload; only overall AQI is
    # comparable as an AQI field, never as a PM/gas concentration.
    return {"aqi": payload.get("aqi"), "pm2_5": None, "pm10": None,
            "co": None, "no2": None, "so2": None, "o3": None,
            "_observed_at": observed_at,
            "_station_name": station.get("name"), "_station_distance_km": distance_km,
            "_raw": data, "_http_status": response.http_status}


PROVIDERS = (("open_meteo", fetch_open_meteo), ("openweather", fetch_openweather), ("waqi", fetch_waqi))
# broad physical plausibility ceilings; values outside these bounds are rejected in stage one
def validate_observation(obs: dict) -> dict:
    """Normalize valid, physically plausible measurements without outlier deletion."""
    clean = {}
    for field in FIELDS:
        value = obs.get(field)
        valid, _ = quality.check_valid_value(value)
        if not valid:
            clean[field] = None
            continue
        value = float(value)
        value_type = "index" if field == "aqi" else "concentration"
        in_range, _ = quality.check_physical_range(field, value, value_type)
        clean[field] = value if in_range else None
    if all(clean[f] is None for f in FIELDS):
        raise ValueError("observation contains no valid measurements")
    return clean


def parse_observation_time(value: Any) -> datetime | None:
    """Parse source times; naive Open-Meteo values are UTC because requests set timezone=UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def canonical_observation_time(value: Any) -> str | None:
    """Normalize a provider observation time to a second-resolution UTC ISO value."""
    parsed = parse_observation_time(value)
    return parsed.replace(microsecond=0).isoformat() if parsed else None


def fuse(city: dict, observations: list[tuple[str, dict]], ingested_at: str,
         *, allow_stale: bool = False) -> dict | None:
    """Fuse valid providers from the latest shared UTC hour; retain disagreements."""
    accepted: list[tuple[str, dict]] = []
    observed_now = datetime.now(timezone.utc)
    parsed_times = {name: parse_observation_time(obs.get("_observed_at")) for name, obs in observations}
    for name, obs in observations:
        try:
            flag, _ = quality.check_freshness(name, obs.get("_observed_at"), observed_now)
            if parsed_times.get(name) is not None and (flag == "ok" or allow_stale):
                accepted.append((name, validate_observation(obs)))
        except ValueError:
            continue
    if not accepted:
        return None
    reference_time = max(parsed_times[name] for name, _ in accepted)
    reference_hour = reference_time.replace(minute=0, second=0, microsecond=0)
    accepted = [(name,row) for name,row in accepted
                if parsed_times[name].replace(minute=0,second=0,microsecond=0)==reference_hour]
    spreads, agreements, result = {}, {}, {}
    dq_rows = []
    for field in FIELDS:
        vals = [row[field] for _, row in accepted if row[field] is not None]
        center, spread, agreement = quality.check_agreement(vals)
        result_value = center
        # A single source remains valid; agreement is not assessable.
        if agreement == "insufficient" and vals:
            agreement = "single_source"
        result[field] = result_value
        spreads[field], agreements[field] = spread, agreement
        if vals:
            dq_rows.append((field, agreement, spread))
    used = sorted({name for name, row in accepted if any(row[f] is not None for f in FIELDS)})
    if not used:
        return None
    observed_time = reference_hour.isoformat()
    used_times = {name: parsed_times[name].replace(microsecond=0).isoformat()
                  for name in used if parsed_times.get(name) is not None}
    valid_fields = sum(value is not None for value in result.values())
    return {"city_name": city["name"], "country": city["country"], "timestamp": observed_time,
            "observed_at": observed_time, "ingested_at": ingested_at,
            "collected_at": ingested_at, "source_times": json.dumps(used_times, sort_keys=True),
            **result, "source_count": len(used), "sources": ",".join(used),
            "quality_score": round(valid_fields / len(FIELDS) * 100, 1),
            "field_coverage": round(valid_fields / len(FIELDS) * 100, 1),
            "source_coverage": round(len(used) / len(PROVIDERS) * 100, 1),
            "spread": json.dumps(spreads, sort_keys=True),
            "agreement_flag": json.dumps(agreements, sort_keys=True),
            "quality_flag": "stale" if allow_stale else "ok",
            "_dq_agreement": dq_rows, "conflicts": "[]"}


SILVER_NAMES = {"pm2_5": "pm2_5", "pm25": "pm2_5", "pm10": "pm10",
                "carbon_monoxide": "co", "co": "co", "nitrogen_dioxide": "no2", "no2": "no2",
                "sulphur_dioxide": "so2", "sulfur_dioxide": "so2", "so2": "so2", "ozone": "o3", "o3": "o3"}


def silver_rows(provider: str, payload: dict[str, Any], observed_at: str | None,
                ingested_at: str, run_id: str) -> tuple[list[tuple], int, int, int]:
    """Flatten provider payloads without averaging; indices never masquerade as concentrations."""
    rows = []
    normalized_observed_at = canonical_observation_time(observed_at)
    if normalized_observed_at is None:
        # Without a valid event time the row cannot be safely deduplicated or
        # placed in hourly marts. Its untouched API payload remains in Bronze.
        return [], 1, 0, 0
    flag, _ = quality.check_freshness(provider, normalized_observed_at)
    invalid_count = range_count = stale_count = 0

    def add(name: str, raw_value: Any, unit: str, value_type: str) -> None:
        nonlocal invalid_count, range_count, stale_count
        valid, _ = quality.check_valid_value(raw_value)
        if not valid:
            invalid_count += 1
            return
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            invalid_count += 1
            return
        in_range, _ = quality.check_physical_range(name, value, value_type)
        if not in_range:
            range_count += 1
            return
        stale_count += flag == "stale"
        rows.append((name, value, unit, value_type, flag))

    if provider == "open_meteo":
        data = payload.get("current", {})
        units = payload.get("current_units", {})
        for source_name, raw_value in data.items():
            pollutant = SILVER_NAMES.get(source_name)
            if pollutant:
                add(pollutant, raw_value, units.get(source_name, CONCENTRATION_UNIT), "concentration")
        for index_name in ("european_aqi", "us_aqi"):
            if index_name in data:
                add(index_name, data[index_name], "index", "index")
    elif provider == "openweather":
        items = payload.get("list", [])
        if items:
            item = items[0]
            for source_name, raw_value in item.get("components", {}).items():
                pollutant = SILVER_NAMES.get(source_name)
                if pollutant:
                    add(pollutant, raw_value, CONCENTRATION_UNIT, "concentration")
            add("aqi", item.get("main", {}).get("aqi"), "OpenWeather AQI (1–5)", "index")
    elif provider == "waqi":
        data = payload.get("data", {})
        add("aqi", data.get("aqi"), "AQI", "index")
        iaqi_names = {"pm25": "pm2_5", "pm10": "pm10", "co": "co", "no2": "no2",
                      "so2": "so2", "o3": "o3"}
        for source_name, item in data.get("iaqi", {}).items():
            if source_name in iaqi_names and isinstance(item, dict):
                add(iaqi_names[source_name], item.get("v"), "WAQI IAQI", "index")
    return ([(provider, pollutant, value, unit, value_type, normalized_observed_at, ingested_at,
              run_id, quality_flag) for pollutant, value, unit, value_type, quality_flag in rows],
            invalid_count, range_count, stale_count)


def store_silver_rows(conn: sqlite3.Connection, city: dict, rows: list[tuple], bronze_id: int) -> None:
    """Idempotently upsert Silver while retaining each run's lineage separately."""
    records = [(city["name"], city["country"], *row, bronze_id) for row in rows]
    conn.executemany("""INSERT INTO silver_measurements
        (city,country,provider,pollutant,value,unit,value_type,observed_at,ingested_at,run_id,quality_flag,bronze_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT DO UPDATE SET country=excluded.country,value=excluded.value,
        unit=excluded.unit,value_type=excluded.value_type,ingested_at=excluded.ingested_at,
        run_id=excluded.run_id,quality_flag=excluded.quality_flag,bronze_id=excluded.bronze_id""", records)
    conn.executemany("""INSERT OR IGNORE INTO silver_measurement_lineage
        (city,provider,pollutant,observed_at,run_id,ingested_at,bronze_id)
        VALUES(?,?,?,?,?,?,?)""", [(row[0],row[2],row[3],row[7],row[9],row[8],row[11])
                                      for row in records])


def record_dq(conn: sqlite3.Connection, run_id: str, check_name: str, scope: str,
              action: str, count: int, details: str) -> None:
    conn.execute("""INSERT INTO dq_results(run_id,check_name,scope,action,count,details,created_at)
        VALUES(?,?,?,?,?,?,?)""", (run_id, check_name, scope, action, count, details,
        datetime.now(timezone.utc).isoformat()))


def upsert_gold(fused: dict, *, current: bool) -> int:
    """Upsert one city/observation Gold fact; a live fact always supersedes stale history."""
    fields = ("city_name", "country", "timestamp", *FIELDS, "source_count", "sources", "quality_score",
              "collected_at", "observed_at", "ingested_at", "source_times", "field_coverage",
              "source_coverage", "spread", "agreement_flag", "conflicts", "run_id", "quality_flag")
    canonical = json.dumps({field: fused[field] for field in FIELDS}, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    observed = parse_observation_time(fused["timestamp"])
    bucket = int(observed.timestamp() if observed else time.time()) // 300
    all_columns = (*fields, "data_hash", "time_bucket")
    placeholders = ",".join("?" for _ in all_columns)
    updates = ",".join(f"{name}=excluded.{name}" for name in all_columns
                        if name not in {"city_name", "observed_at"})
    where = "" if current else " WHERE air_quality.quality_flag IS NULL OR air_quality.quality_flag!='ok'"
    values = (*tuple(fused[name] for name in fields), digest, bucket)
    query = (f"INSERT INTO air_quality ({','.join(all_columns)}) VALUES ({placeholders}) "
             f"ON CONFLICT(city_name,observed_at) DO UPDATE SET {updates}{where}")
    with closing(connect(DB_PATH)) as conn, conn:
        try:
            return conn.execute(query, values).rowcount
        except sqlite3.IntegrityError:
            # A second legacy five-minute hash key may collide on adjacent times.
            # Keep that existing Gold fact rather than aborting the collection run.
            cursor = conn.execute(f"INSERT OR IGNORE INTO air_quality ({','.join(all_columns)}) VALUES ({placeholders})", values)
            return cursor.rowcount


def safe_error(exc: Exception) -> str:
    """Return only a safe class/status; exception messages may contain URLs or keys."""
    if isinstance(exc, ProviderError) and exc.http_status is not None:
        return f"{type(exc).__name__}: HTTP {exc.http_status}"
    return f"{type(exc).__name__}: request or provider processing failed"


def collect_city(city: dict, run_id: str | None = None) -> dict | None:
    run_id = run_id or str(uuid4())
    observations = []
    with ThreadPoolExecutor(max_workers=max(1, len(PROVIDERS)), thread_name_prefix="air-provider") as executor:
        futures = {name: executor.submit(provider, city) for name, provider in PROVIDERS}
    with closing(connect(DB_PATH)) as conn, conn:
        for name, provider in PROVIDERS:
            try:
                data = futures[name].result()
                provider_ingested_at = datetime.now(timezone.utc).isoformat()
                raw_payload = data.get("_raw", data)
                raw_json = json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"))
                units_ok, units_details = quality.check_units(name, raw_payload)
                if not units_ok:
                    raise ProviderError(f"Unit validation failed: {units_details}", payload=raw_payload,
                                         http_status=data.get("_http_status"))
                bronze = conn.execute("""INSERT INTO bronze_responses
                    (run_id,provider,city,country,ingested_at,http_status,payload_json,status,error,error_class)
                    VALUES(?,?,?,?,?,?,?,?,NULL,NULL)""",
                    (run_id, name, city["name"], city["country"], provider_ingested_at,
                     data.get("_http_status"), raw_json, "ok"))
                bronze_id = bronze.lastrowid
                rows, invalid_count, range_count, stale_count = silver_rows(
                    name, raw_payload, data.get("_observed_at"), provider_ingested_at, run_id)
                store_silver_rows(conn, city, rows, bronze_id)
                observations.append((name, data))
                scope = f"{city['name']}:{name}"
                record_dq(conn, run_id, "valid_value", scope, "reject" if invalid_count else "none",
                          invalid_count, f"invalid/missing numeric value or observation timestamp; {units_details}")
                record_dq(conn, run_id, "physical_range", scope, "reject" if range_count else "none",
                          range_count, "negative or beyond deliberately broad physical ceiling")
                record_dq(conn, run_id, "unit", scope, "none", 0, units_details)
                record_dq(conn, run_id, "freshness", scope, "mark_stale" if stale_count else "none",
                          stale_count, f"provider-specific age limit: {quality.FRESHNESS_LIMIT_MINUTES.get(name, 90)} minutes")
                conn.execute("""INSERT INTO raw_observations
                    (city_name,country,provider,fetched_at,payload,status,observed_at,ingested_at,station_name,station_distance_km)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                             (city["name"], city["country"], name, provider_ingested_at,
                              json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":")), "ok",
                              canonical_observation_time(data.get("_observed_at")), provider_ingested_at,
                              data.get("_station_name"), data.get("_station_distance_km")))
            except Exception as exc:
                provider_ingested_at = datetime.now(timezone.utc).isoformat()
                error = safe_error(exc)
                payload = exc.payload if isinstance(exc, ProviderError) and exc.payload is not None else {}
                http_status = exc.http_status if isinstance(exc, ProviderError) else None
                conn.execute("""INSERT INTO bronze_responses
                    (run_id,provider,city,country,ingested_at,http_status,payload_json,status,error,error_class)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, name, city["name"], city["country"], provider_ingested_at,
                     http_status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "fail", error,
                     type(exc).__name__))
                scope = f"{city['name']}:{name}"
                check_name = "unit" if isinstance(exc, ProviderError) and str(exc).startswith("Unit validation failed") else "provider_request"
                record_dq(conn, run_id, check_name, scope, "fail", 1, error)
                if check_name == "unit":
                    record_dq(conn, run_id, "valid_value", scope, "none", 0, "skipped: provider response rejected for unit change")
                    record_dq(conn, run_id, "physical_range", scope, "none", 0, "skipped: provider response rejected for unit change")
                    record_dq(conn, run_id, "freshness", scope, "none", 0, "skipped: provider response rejected for unit change")
                    record_dq(conn, run_id, "source_agreement", city["name"], "none", 0, "skipped: provider response rejected before Gold")
                else:
                    record_dq(conn, run_id, "unit", scope, "none", 0, "skipped: provider request did not yield a payload")
                    record_dq(conn, run_id, "valid_value", scope, "none", 0, "skipped: provider request did not yield a payload")
                    record_dq(conn, run_id, "physical_range", scope, "none", 0, "skipped: provider request did not yield a payload")
                    record_dq(conn, run_id, "freshness", scope, "none", 0, "skipped: provider request did not yield a payload")
                    record_dq(conn, run_id, "source_agreement", city["name"], "none", 0, "skipped: provider request did not yield a Gold measurement")
                conn.execute("""INSERT INTO raw_observations
                    (city_name,country,provider,fetched_at,payload,status,error,ingested_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                             (city["name"], city["country"], name, provider_ingested_at, "{}", "error",
                              error, provider_ingested_at))
    ingested_at = datetime.now(timezone.utc).isoformat()
    fused = fuse(city, observations, ingested_at)
    if fused:
        fused["run_id"] = run_id
        fused["duplicate"] = upsert_gold(fused, current=True) == 0
        with closing(connect(DB_PATH)) as conn, conn:
            for pollutant, agreement, spread in fused.get("_dq_agreement", []):
                flagged = agreement == "low"
                record_dq(conn, run_id, "source_agreement", f"{city['name']}:{pollutant}",
                          "flag" if flagged else "none", int(flagged),
                          f"agreement_flag={agreement}; spread={spread}; source values retained")
    else:
        with closing(connect(DB_PATH)) as conn, conn:
            record_dq(conn, run_id, "source_agreement", city["name"], "none", 0,
                      "no fresh valid measurement available for Gold comparison")
    return fused


def collect_all() -> tuple[int, int]:
    init_db()
    cities = json.loads((ROOT / "config" / "cities.json").read_text(encoding="utf-8"))
    succeeded = 0
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    with closing(connect(DB_PATH)) as conn, conn:
        conn.execute("INSERT INTO collection_runs(run_id,started_at,status,total_pairs,failed_pairs) VALUES(?,?,?,?,?)",
                     (run_id, started_at, "running", len(cities) * len(PROVIDERS), 0))
    for city in cities:
        result = collect_city(city, run_id=run_id)
        if result and not result.get("duplicate", False):
            succeeded += 1
        time.sleep(0.15)  # modest provider rate limiting
    with closing(connect(DB_PATH)) as conn, conn:
        failed_pairs = conn.execute("SELECT COUNT(*) FROM bronze_responses WHERE run_id=? AND status!='ok'", (run_id,)).fetchone()[0]
        total_pairs = len(cities) * len(PROVIDERS)
        status, degraded = quality.check_run_completeness(total_pairs, failed_pairs)
        if failed_pairs == total_pairs and total_pairs:
            status, degraded = "fail", True
        elif status == "complete":
            status = "ok"
        finished_at = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE collection_runs SET finished_at=?,status=?,failed_pairs=? WHERE run_id=?",
                     (finished_at, status, failed_pairs, run_id))
        record_dq(conn, run_id, "run_completeness", "run", "warn" if degraded else "none",
                  failed_pairs, f"{failed_pairs}/{total_pairs} city/provider pairs failed; run status={status}")
        provider_errors = {provider: count for provider, count in conn.execute(
            "SELECT provider,COUNT(*) FROM bronze_responses WHERE run_id=? AND status!='ok' GROUP BY provider", (run_id,))}
        conn.execute("UPDATE collection_runs SET provider_errors=? WHERE run_id=?",
                     (json.dumps(provider_errors, sort_keys=True), run_id))
    from src.analytics import refresh_analytics
    refresh_analytics(DB_PATH)
    logger.info("collection finished run_id=%s status=%s failed_pairs=%s", run_id, status, failed_pairs)
    return succeeded, len(cities)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s run_id=%(run_id)s %(message)s")
    class RunIdFilter(logging.Filter):
        def filter(self, record):
            if not hasattr(record, "run_id"):
                record.run_id = "-"
            return True
    logging.getLogger().addFilter(RunIdFilter())
    for handler in logging.getLogger().handlers:
        handler.addFilter(RunIdFilter())
    import argparse

    parser = argparse.ArgumentParser(description="Collect current air quality or backfill historical data.")
    parser.add_argument("--backfill-days", type=int, help="Backfill 1–92 historical days, then exit.")
    parser.add_argument("--weather-backfill-days", type=int, help="Optionally store historical hourly weather context, then exit.")
    args = parser.parse_args()
    if args.weather_backfill_days is not None:
        from datetime import timedelta
        from src.analytics import fetch_weather_archive, refresh_analytics
        init_db()
        days = args.weather_backfill_days
        if days < 1:
            parser.error("--weather-backfill-days must be at least 1")
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days - 1)
        run_id = str(uuid4())
        cities = json.loads((ROOT / "config" / "cities.json").read_text(encoding="utf-8"))
        total = 0
        for city in cities:
            total += fetch_weather_archive(city, start.isoformat(), end.isoformat(), run_id)
        refresh_analytics(DB_PATH)
        logger.info("weather backfill run_id=%s days=%s rows=%s cities=%s", run_id, days, total, len(cities))
    elif args.backfill_days is not None:
        from src.backfill import backfill
        result = backfill(args.backfill_days)
        logger.info("backfill finished run_id=%s status=%s gold_rows=%s cities=%s providers=%s failures=%s",
                    result['run_id'], result['status'], result['gold_rows'], result['cities'],
                    ','.join(result['providers']), result['failed_pairs'])
    else:
        ok, total = collect_all()
        logger.info("collection run completed cities_with_new_data=%s total=%s database=%s", ok, total, DB_PATH)
