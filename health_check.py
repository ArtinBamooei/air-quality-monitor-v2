"""Check local v2 project configuration and schema; opt into network probes with --live."""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import argparse
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from src import collector  # noqa: E402 - ROOT is added to sys.path above for direct script execution.
from src.database import connect  # noqa: E402


def report(label: str, state: str, details: str = "") -> bool:
    suffix = f" | {details}" if details else ""
    print(f"[{state}] {label}{suffix}")
    return state == "OK"


def check_project(*, live: bool = False) -> tuple[int, int, int]:
    print("Air Quality Monitor v2 - Health Check")
    print("=" * 42)
    checks_ok = 0
    checks_total = 0
    checks_failed = 0

    def add(label: str, state: str, details: str = "") -> None:
        nonlocal checks_ok, checks_total, checks_failed
        checks_total += 1
        checks_ok += report(label, state, details)
        checks_failed += state == "FAIL"

    cities_path = ROOT / "config" / "cities.json"
    try:
        cities = json.loads(cities_path.read_text(encoding="utf-8"))
        counts = {country: sum(c.get("country") == country for c in cities)
                  for country in ("Iran", "Austria", "Germany")}
        valid = (len(cities) == 15 and counts == {"Iran": 5, "Austria": 5, "Germany": 5}
                 and all(isinstance(c.get("lat"), (int, float)) and isinstance(c.get("lon"), (int, float))
                         for c in cities))
        add("City configuration", "OK" if valid else "FAIL",
            f"{len(cities)} cities; " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    except Exception as exc:
        add("City configuration", "FAIL", str(exc))
        cities = []

    for package in ("requests", "pandas", "plotly", "streamlit"):
        try:
            version = importlib.metadata.version(package)
            add(f"Dependency {package}", "OK", version)
        except importlib.metadata.PackageNotFoundError:
            add(f"Dependency {package}", "FAIL", "not installed")

    try:
        if not collector.DB_PATH.exists():
            raise FileNotFoundError(f"database not found: {collector.DB_PATH.name}")
        with closing(connect(collector.DB_PATH)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            raw_columns = {r[1] for r in conn.execute("PRAGMA table_info(raw_observations)")}
            clean_columns = {r[1] for r in conn.execute("PRAGMA table_info(air_quality)")}
            bronze_columns = {r[1] for r in conn.execute("PRAGMA table_info(bronze_responses)")}
            silver_columns = {r[1] for r in conn.execute("PRAGMA table_info(silver_measurements)")}
            gold_columns = {r[1] for r in conn.execute("PRAGMA table_info(air_quality)")}
            raw_count = conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0]
            clean_count = conn.execute("SELECT COUNT(*) FROM air_quality").fetchone()[0]
            bronze_count = conn.execute("SELECT COUNT(*) FROM bronze_responses").fetchone()[0]
            silver_count = conn.execute("SELECT COUNT(*) FROM silver_measurements").fetchone()[0]
            time_schema_ok = {"observed_at", "ingested_at"}.issubset(raw_columns) and {"observed_at", "ingested_at"}.issubset(clean_columns)
            bronze_ok = {"run_id", "provider", "city", "ingested_at", "http_status", "payload_json"}.issubset(bronze_columns)
            silver_ok = {"city", "provider", "pollutant", "value", "unit", "value_type", "observed_at", "ingested_at", "run_id", "quality_flag"}.issubset(silver_columns)
            silver_key_ok = "ux_silver_observation" in indexes
            dq_ok = {"dq_results", "collection_runs"}.issubset(tables) and {"spread", "agreement_flag"}.issubset(gold_columns)
            db_ok = ({"raw_observations", "air_quality", "bronze_responses", "silver_measurements"}.issubset(tables)
                     and {"ux_air_quality_dedup", "ux_air_city_observed"}.issubset(indexes)
                     and time_schema_ok and bronze_ok and silver_ok and silver_key_ok and dq_ok)
        add("SQLite database and schema", "OK" if db_ok else "FAIL",
            f"{collector.DB_PATH.name}; {bronze_count} Bronze / {silver_count} Silver / {raw_count} legacy raw / {clean_count} Gold rows")
    except Exception as exc:
        add("SQLite database and schema", "FAIL", str(exc))

    # Network probes are opt-in and never mutate the project database.
    if live and cities:
        probe_city = cities[0]
        for provider_name, provider in collector.PROVIDERS:
            if provider_name == "openweather" and not os.getenv("OPENWEATHER_API_KEY"):
                add("API OpenWeather", "SKIP", "OPENWEATHER_API_KEY is not set")
                continue
            if provider_name == "waqi" and not os.getenv("WAQI_API_TOKEN"):
                add("API WAQI", "SKIP", "WAQI_API_TOKEN is not set")
                continue
            try:
                payload = provider(probe_city)
                cleaned = collector.validate_observation(payload)
                valid_values = sum(value is not None for value in cleaned.values())
                add(f"API {provider_name}", "OK", f"Tehran probe returned {valid_values} valid fields")
            except Exception as exc:
                add(f"API {provider_name}", "FAIL", collector.safe_error(exc))
    elif cities:
        for provider_name, _ in collector.PROVIDERS:
            add(f"API {provider_name}", "SKIP", "network probe disabled; pass --live to opt in")

    print("=" * 42)
    print(f"Result: {checks_ok}/{checks_total} checks passed (SKIP means credentials/config are missing).")
    return checks_ok, checks_total, checks_failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make provider API probes (no data is stored)")
    args = parser.parse_args()
    _, _, failed = check_project(live=args.live)
    # Missing optional API credentials produce SKIP and do not fail the process.
    sys.exit(1 if failed else 0)
