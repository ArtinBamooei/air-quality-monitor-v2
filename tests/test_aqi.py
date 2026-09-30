import pytest

from src.aqi import calculate_pm25_aqi


@pytest.mark.parametrize(("pm25", "expected"), [(9.0, 50), (9.1, 51), (35.4, 100), (35.5, 101)])
def test_2024_epa_pm25_known_breakpoints(pm25, expected):
    assert calculate_pm25_aqi(pm25) == expected


def test_aqi_rejects_negative_concentrations():
    with pytest.raises(ValueError):
        calculate_pm25_aqi(-0.1)
