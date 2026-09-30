"""Historical backfill through Bronze, Silver, DQ, and stale Gold history."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import time
from typing import Any
from uuid import uuid4

from src import collector, quality
from src.database import connect

OM_NAMES = {"pm2_5": "pm2_5", "pm10": "pm10", "co": "carbon_monoxide",
            "no2": "nitrogen_dioxide", "so2": "sulphur_dioxide", "o3": "ozone"}


def _utc_iso(value: str) -> str:
    parsed = collector.parse_observation_time(value)
    if parsed is None:
        return value
    return parsed.replace(microsecond=0).isoformat()


def _open_meteo_url_params(city: dict[str, Any], days: int) -> dict[str, Any]:
    return {"latitude": city["lat"], "longitude": city["lon"],
            "hourly": ",".join((*OM_NAMES.values(), "european_aqi", "us_aqi")), "past_days": days,
            "forecast_days": 0, "timezone": "UTC"}


def _open_meteo_samples(payload: dict[str, Any]) -> list[tuple[str, dict, dict]]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    samples = []
    for index, time_value in enumerate(times):
        current = {"time": time_value}
        normalized = {"aqi": None}
        for canonical, source in OM_NAMES.items():
            values = hourly.get(source, [])
            value = values[index] if index < len(values) else None
            current[source] = value
            normalized[canonical] = value
        for index_name in ("european_aqi", "us_aqi"):
            index_values = hourly.get(index_name, [])
            if index < len(index_values):
                current[index_name] = index_values[index]
        observed_at = _utc_iso(time_value)
        normalized["_observed_at"] = observed_at
        sample_payload = {"current": current, "current_units": payload.get("hourly_units", {})}
        samples.append((observed_at, normalized, sample_payload))
    return samples


def _openweather_samples(payload: dict[str, Any]) -> list[tuple[str, dict, dict]]:
    samples = []
    for item in payload.get("list", []):
        observed_at = datetime.fromtimestamp(item["dt"], timezone.utc).replace(microsecond=0).isoformat()
        components = item.get("components", {})
        normalized = {"aqi": None, **{name: components.get(name) for name in collector.FIELDS if name != "aqi"}}
        normalized["_observed_at"] = observed_at
        sample_payload = {"list": [item]}
        samples.append((observed_at, normalized, sample_payload))
    return samples


def _record_bronze(conn: sqlite3.Connection, run_id: str, city: dict, provider: str,
                   payload: dict, ingested_at: str, http_status: int | None,
                   status: str, error: str | None = None) -> int:
    cursor = conn.execute("""INSERT INTO bronze_responses
        (run_id,provider,city,country,ingested_at,http_status,payload_json,status,error,error_class)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (run_id, provider, city["name"], city["country"], ingested_at,
        http_status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), status, error,
        error.partition(":")[0] if error else None))
    return cursor.lastrowid


def _store_provider_payload(conn: sqlite3.Connection, city: dict, provider: str, run_id: str,
                            payload: dict, http_status: int, samples: list[tuple[str, dict, dict]],
                            ingested_at: str) -> tuple[dict[datetime, dict[str, dict]], bool]:
    units_ok, units_details = quality.check_units(provider, payload)
    scope = f"{city['name']}:{provider}"
    if not units_ok:
        error = f"ProviderError: Unit validation failed: {units_details}"
        _record_bronze(conn, run_id, city, provider, payload, ingested_at, http_status, "fail", error)
        collector.record_dq(conn, run_id, "unit", scope, "fail", 1, units_details)
        for check in ("valid_value", "physical_range", "freshness"):
            collector.record_dq(conn, run_id, check, scope, "none", 0, "skipped: unit validation failed")
        return {}, False

    bronze_id = _record_bronze(conn, run_id, city, provider, payload, ingested_at, http_status, "ok")
    indexed: dict[datetime, dict[str, dict]] = {}
    invalid_count = range_count = stale_count = 0
    conn.execute("""INSERT INTO raw_observations
            (city_name,country,provider,fetched_at,payload,status,observed_at,ingested_at)
            VALUES(?,?,?,?,?,?,?,?)""", (city["name"], city["country"], provider, ingested_at,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "ok",
            samples[-1][0] if samples else None, ingested_at))

    for observed_at, normalized, sample_payload in samples:
        rows, invalid, out_of_range, stale = collector.silver_rows(
            provider, sample_payload, observed_at, ingested_at, run_id)
        invalid_count += invalid
        range_count += out_of_range
        stale_count += stale
        collector.store_silver_rows(conn, city, rows, bronze_id)
        parsed = collector.parse_observation_time(observed_at)
        if parsed and any(normalized.get(field) is not None for field in collector.FIELDS):
            indexed.setdefault(parsed, {})[provider] = normalized

    collector.record_dq(conn, run_id, "unit", scope, "none", 0, units_details)
    collector.record_dq(conn, run_id, "valid_value", scope, "reject" if invalid_count else "none",
                        invalid_count, "invalid/missing values skipped; Bronze response retained")
    collector.record_dq(conn, run_id, "physical_range", scope, "reject" if range_count else "none",
                        range_count, "negative or beyond broad physical ceiling; Bronze retained")
    collector.record_dq(conn, run_id, "freshness", scope, "mark_stale" if stale_count else "none",
                        stale_count, f"historical values retained in Silver; policy limit {quality.FRESHNESS_LIMIT_MINUTES.get(provider, 90)} minutes")
    return indexed, True


def _fetch_open_meteo(city: dict, days: int):
    response = collector._get("https://air-quality-api.open-meteo.com/v1/air-quality",
                              _open_meteo_url_params(city, days))
    samples = _open_meteo_samples(response.payload)
    if not samples:
        raise collector.ProviderError("Open-Meteo history returned no hourly samples",
                                      payload=response.payload, http_status=response.http_status)
    return response, samples


def _fetch_openweather_chunks(city: dict, days: int):
    key = os.getenv("OPENWEATHER_API_KEY")
    if not key:
        return
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=7), end)
        response = collector._get("https://api.openweathermap.org/data/2.5/air_pollution/history", {
            "lat": city["lat"], "lon": city["lon"], "start": int(cursor.timestamp()),
            "end": int(chunk_end.timestamp()), "appid": key,
        })
        yield response, _openweather_samples(response.payload)
        cursor = chunk_end
        time.sleep(0.12)


def _store_gold_history(city: dict, observations: dict[datetime, dict[str, dict]], run_id: str) -> int:
    stored = 0
    for observed, sources in sorted(observations.items()):
        ingested_at = datetime.now(timezone.utc).isoformat()
        fused = collector.fuse(city, list(sources.items()), ingested_at, allow_stale=True)
        if not fused:
            continue
        fused["run_id"] = run_id
        fused["quality_flag"] = "stale"  # Historical facts belong to trends, never current-status cards.
        # A replay can update stale history but cannot downgrade a live Gold row.
        stored += max(collector.upsert_gold(fused, current=False), 0)
        with closing(connect(collector.DB_PATH)) as conn, conn:
            for pollutant, agreement, spread in fused.get("_dq_agreement", []):
                flagged = agreement == "low"
                collector.record_dq(conn, run_id, "source_agreement", f"{city['name']}:{pollutant}",
                                    "flag" if flagged else "none", int(flagged),
                                    f"historical agreement={agreement}; spread={spread}; sources retained")
    return stored


def backfill(days: int = 92, *, include_openweather: bool = True) -> dict[str, Any]:
    """Backfill up to Open-Meteo's documented 92-day window into all v2 layers."""
    if not isinstance(days, int) or not 1 <= days <= 92:
        raise ValueError("days must be an integer from 1 to 92")
    collector.init_db()
    cities = json.loads((collector.ROOT / "config" / "cities.json").read_text(encoding="utf-8"))
    providers = ["open_meteo"]
    if include_openweather and os.getenv("OPENWEATHER_API_KEY"):
        providers.append("openweather")
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    total_pairs = len(cities) * len(providers)
    with closing(connect(collector.DB_PATH)) as conn, conn:
        conn.execute("INSERT INTO collection_runs(run_id,started_at,status,total_pairs,failed_pairs) VALUES(?,?,?,?,0)",
                     (run_id, started_at, "running", total_pairs))

    failed_pairs: set[tuple[str, str]] = set()
    gold_rows = 0
    for city in cities:
        observations: dict[datetime, dict[str, dict]] = {}
        try:
            response, samples = _fetch_open_meteo(city, days)
            ingested_at = datetime.now(timezone.utc).isoformat()
            with closing(connect(collector.DB_PATH)) as conn, conn:
                incoming, unit_ok = _store_provider_payload(conn, city, "open_meteo", run_id,
                    response.payload, response.http_status, samples, ingested_at)
                if not unit_ok:
                    failed_pairs.add((city["name"], "open_meteo"))
            for observed, sources in incoming.items():
                observations.setdefault(observed, {}).update(sources)
        except Exception as exc:
            failed_pairs.add((city["name"], "open_meteo"))
            error = collector.safe_error(exc)
            payload = exc.payload if isinstance(exc, collector.ProviderError) and exc.payload is not None else {}
            status = exc.http_status if isinstance(exc, collector.ProviderError) else None
            with closing(connect(collector.DB_PATH)) as conn, conn:
                _record_bronze(conn, run_id, city, "open_meteo", payload,
                    datetime.now(timezone.utc).isoformat(), status, "fail", error)
                collector.record_dq(conn, run_id, "provider_request", f"{city['name']}:open_meteo", "fail", 1, error)

        if "openweather" in providers:
            try:
                for response, samples in _fetch_openweather_chunks(city, days):
                    ingested_at = datetime.now(timezone.utc).isoformat()
                    with closing(connect(collector.DB_PATH)) as conn, conn:
                        incoming, unit_ok = _store_provider_payload(conn, city, "openweather", run_id,
                            response.payload, response.http_status, samples, ingested_at)
                    if not unit_ok:
                        failed_pairs.add((city["name"], "openweather"))
                    for observed, sources in incoming.items():
                        observations.setdefault(observed, {}).update(sources)
            except Exception as exc:
                failed_pairs.add((city["name"], "openweather"))
                error = collector.safe_error(exc)
                payload = exc.payload if isinstance(exc, collector.ProviderError) and exc.payload is not None else {}
                status = exc.http_status if isinstance(exc, collector.ProviderError) else None
                with closing(connect(collector.DB_PATH)) as conn, conn:
                    _record_bronze(conn, run_id, city, "openweather", payload,
                        datetime.now(timezone.utc).isoformat(), status, "fail", error)
                    collector.record_dq(conn, run_id, "provider_request", f"{city['name']}:openweather", "fail", 1, error)

        gold_rows += _store_gold_history(city, observations, run_id)
        time.sleep(0.12)

    failed_count = len(failed_pairs)
    status, degraded = quality.check_run_completeness(total_pairs, failed_count)
    if failed_count == total_pairs and total_pairs:
        status, degraded = "fail", True
    elif status == "complete":
        status = "ok"
    with closing(connect(collector.DB_PATH)) as conn, conn:
        conn.execute("UPDATE collection_runs SET finished_at=?,status=?,failed_pairs=? WHERE run_id=?",
            (datetime.now(timezone.utc).isoformat(), status, failed_count, run_id))
        collector.record_dq(conn, run_id, "run_completeness", "run", "warn" if degraded else "none",
            failed_count, f"{failed_count}/{total_pairs} provider-city pairs failed; backfill status={status}")
        errors_by_provider = {p: n for p, n in conn.execute(
            "SELECT provider,COUNT(*) FROM bronze_responses WHERE run_id=? AND status!='ok' GROUP BY provider", (run_id,))}
        conn.execute("""UPDATE collection_runs SET provider_errors=?,
            bronze_rows=(SELECT COUNT(*) FROM bronze_responses WHERE run_id=?),
            silver_rows=(SELECT COUNT(*) FROM silver_measurements WHERE run_id=?),
            finished_at=? WHERE run_id=?""",
            (json.dumps(errors_by_provider, sort_keys=True), run_id, run_id, datetime.now(timezone.utc).isoformat(), run_id))
    from src.analytics import refresh_analytics
    refresh_analytics(collector.DB_PATH)
    return {"run_id": run_id, "days": days, "providers": providers, "cities": len(cities),
            "gold_rows": gold_rows, "failed_pairs": failed_count, "status": status}
