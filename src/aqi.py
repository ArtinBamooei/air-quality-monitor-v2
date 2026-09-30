"""US EPA PM2.5 AQI calculation using the 2024 24-hour breakpoints."""
from __future__ import annotations

from math import floor, isfinite

# (concentration low/high, AQI low/high), PM2.5 µg/m³ 24-hour mean.
PM25_BREAKPOINTS_2024 = (
    (0.0, 9.0, 0, 50),
    (9.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
)


def calculate_pm25_aqi(concentration_ug_m3: float) -> int:
    """Calculate the EPA PM2.5 AQI from a 24-hour mean, not a single-hour reading."""
    concentration = float(concentration_ug_m3)
    if not isfinite(concentration) or concentration < 0:
        raise ValueError("PM2.5 concentration must be finite and non-negative")
    concentration = floor(concentration * 10) / 10  # EPA truncates PM2.5 to one decimal place.
    for c_low, c_high, i_low, i_high in PM25_BREAKPOINTS_2024:
        if c_low <= concentration <= c_high:
            index = (i_high - i_low) / (c_high - c_low) * (concentration - c_low) + i_low
            return floor(index + 0.5)
    # EPA's public AQI scale ends at 500; concentrations beyond the table cap there.
    return 500
