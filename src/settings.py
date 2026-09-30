"""Typed runtime settings for the v2 pipeline."""
from dataclasses import dataclass, field
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_path: Path = ROOT / "air_quality_v2.db"
    request_timeout_seconds: float = 12.0
    retry_attempts: int = 3
    freshness_minutes: dict[str, int] = field(default_factory=lambda: {
        "open_meteo": 45, "openweather": 90, "waqi": 120})
    concentration_max: dict[str, float] = field(default_factory=lambda: {
        "pm2_5": 5_000.0, "pm10": 10_000.0, "co": 10_000_000.0,
        "no2": 100_000.0, "so2": 100_000.0, "o3": 100_000.0})
    backfill_max_days: int = 92

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(database_path=Path(os.getenv("AIR_QUALITY_DB", str(ROOT / "air_quality_v2.db"))),
                   request_timeout_seconds=float(os.getenv("HTTP_TIMEOUT", "12")),
                   retry_attempts=int(os.getenv("HTTP_RETRIES", "3")),
                   backfill_max_days=92)


SETTINGS = Settings.from_env()
