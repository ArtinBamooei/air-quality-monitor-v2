import pytest

from src.units import convert_concentration


def test_milligrams_convert_to_micrograms():
    assert convert_concentration(0.0354, "mg/m³", "µg/m³") == pytest.approx(35.4)


def test_microgram_unit_aliases_are_supported():
    assert convert_concentration(35.4, "μg/m³", "mg/m³") == pytest.approx(0.0354)
