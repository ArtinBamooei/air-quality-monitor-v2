"""Small, explicit data-quality checks used by the ingestion and Gold layers."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from numbers import Real
from statistics import median
from typing import Any, Iterable
from src.settings import SETTINGS

# Deliberately broad ceilings: these catch impossible magnitudes while retaining
# severe but plausible events, including dust storms. AQI indices are not concentration.
PHYSICAL_MAX = SETTINGS.concentration_max
EXPECTED_UNITS = {"µg/m³", "μg/m³", "ug/m3", "µg/m3", "μg/m3"}
FRESHNESS_LIMIT_MINUTES = SETTINGS.freshness_minutes


def check_valid_value(value: Any) -> tuple[bool, str]:
    """Reject missing, non-numeric, NaN, and infinite values."""
    if value is None or isinstance(value, bool) or not isinstance(value, Real):
        return False, "missing or non-numeric value"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False, "value is not numeric"
    if not isfinite(number):
        return False, "value is not finite"
    return True, "valid finite number"


def check_physical_range(pollutant: str, value: float, value_type: str = "concentration") -> tuple[bool, str]:
    """Reject negative values and only exceptionally impossible concentration magnitudes."""
    if value < 0:
        return False, "negative measurement"
    if value_type == "concentration" and pollutant in PHYSICAL_MAX and value > PHYSICAL_MAX[pollutant]:
        return False, f"exceeds broad physical ceiling {PHYSICAL_MAX[pollutant]:g}"
    if value_type == "index" and value > 100_000:
        return False, "index exceeds broad numeric ceiling"
    return True, "within broad physical range"


def check_units(provider: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """Check declared units; providers that do not include units use documented API units."""
    if provider != "open_meteo":
        return True, "provider response has implicit documented units"
    declared = payload.get("current_units") or payload.get("hourly_units") or {}
    if not declared:
        return True, "response does not declare units; documented units assumed"
    wrong = {name: unit for name, unit in declared.items()
             if name in {"pm2_5", "pm10", "carbon_monoxide", "nitrogen_dioxide",
                         "sulphur_dioxide", "ozone"} and unit not in EXPECTED_UNITS}
    return (False, f"unexpected declared units: {wrong}") if wrong else (True, "declared pollutant units are µg/m³")


def check_freshness(provider: str, observed_at: str | None,
                    now: datetime | None = None) -> tuple[str, float | None]:
    """Return ok/stale and age in minutes using provider-specific operational limits."""
    if not observed_at:
        return "stale", None
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "stale", None
    observed = observed.replace(tzinfo=timezone.utc) if observed.tzinfo is None else observed.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = (now.astimezone(timezone.utc) - observed).total_seconds() / 60
    limit = FRESHNESS_LIMIT_MINUTES.get(provider, 90)
    return ("ok" if age <= limit else "stale"), age


def check_run_completeness(total_pairs: int, failed_pairs: int) -> tuple[str, bool]:
    """Mark degraded only when more than half of city/provider pairs failed."""
    degraded = total_pairs > 0 and failed_pairs / total_pairs > 0.5
    return ("degraded" if degraded else "complete"), degraded


def check_agreement(values: Iterable[float]) -> tuple[float | None, float | None, str]:
    """Use median (mean for two, which equals median); retain every provider value."""
    values = [float(value) for value in values]
    if not values:
        return None, None, "insufficient"
    center = median(values)
    spread = max(values) - min(values)
    low = len(values) >= 2 and spread > max(abs(center) * 0.5, 2.0)
    return center, spread, "low" if low else "ok"
