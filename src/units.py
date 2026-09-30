"""Explicit concentration-unit conversions; never infer gas conversions from ppb."""
from __future__ import annotations

from math import isfinite

UNIT_ALIASES = {
    "µg/m³": "ug/m3", "μg/m³": "ug/m3", "ug/m³": "ug/m3", "ug/m3": "ug/m3",
    "µg/m3": "ug/m3", "μg/m3": "ug/m3",
    "mg/m³": "mg/m3", "mg/m3": "mg/m3",
    "g/m³": "g/m3", "g/m3": "g/m3",
}
TO_UG_M3 = {"ug/m3": 1.0, "mg/m3": 1_000.0, "g/m3": 1_000_000.0}


def convert_concentration(value: float, from_unit: str, to_unit: str = "µg/m³") -> float:
    """Convert mass concentration among g/m³, mg/m³, and µg/m³."""
    number = float(value)
    if not isfinite(number):
        raise ValueError("concentration must be finite")
    try:
        source = UNIT_ALIASES[from_unit.strip()]
        target = UNIT_ALIASES[to_unit.strip()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"unsupported concentration unit: {from_unit!r} -> {to_unit!r}") from exc
    return number * TO_UG_M3[source] / TO_UG_M3[target]
