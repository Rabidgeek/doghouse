"""Unit tests for :mod:`doghouse.normalize`."""

from __future__ import annotations

import pytest

from doghouse.normalize import normalize

# Golden raw frame lifted verbatim from the project spec.
_GOLDEN_RAW: dict[str, str] = {
    "V": "12800",
    "VPV": "33550",
    "PPV": "76",
    "I": "5900",
    "CS": "3",
    "MPPT": "2",
    "ERR": "0",
    "H19": "145",
    "H20": "25",
    "H21": "189",
    "H22": "31",
    "H23": "210",
    "LOAD": "ON",
    "IL": "0",
}


def test_golden_frame_matches_spec_example() -> None:
    out = normalize(_GOLDEN_RAW)
    assert out["V"] == pytest.approx(12.8)
    assert out["VPV"] == pytest.approx(33.55)
    assert out["I"] == pytest.approx(5.9)
    assert out["IL"] == pytest.approx(0.0)
    assert out["PPV"] == pytest.approx(76.0)
    assert out["H19_kwh"] == pytest.approx(1.45)
    assert out["H20_kwh"] == pytest.approx(0.25)
    assert out["H21"] == pytest.approx(189.0)
    assert out["H22_kwh"] == pytest.approx(0.31)
    assert out["H23"] == pytest.approx(210.0)
    assert out["P_battery_watts"] == pytest.approx(75.52, rel=1e-3)
    assert out["charge_state_name"] == "bulk"
    assert out["mppt_state_name"] == "mpp-tracker-active"
    assert out["error_name"] is None


def test_input_not_mutated() -> None:
    snapshot = dict(_GOLDEN_RAW)
    normalize(_GOLDEN_RAW)
    assert snapshot == _GOLDEN_RAW


def test_unknown_fields_ignored() -> None:
    out = normalize({"V": "12800", "FOO": "bar", "LOAD": "ON"})
    assert out == {"V": pytest.approx(12.8)}


def test_negative_current_preserved() -> None:
    out = normalize({"V": "12800", "I": "-1500"})
    assert out["I"] == pytest.approx(-1.5)
    assert out["P_battery_watts"] == pytest.approx(-19.2, rel=1e-3)


def test_missing_fields_omitted() -> None:
    out = normalize({"V": "12800"})
    assert "I" not in out
    assert "P_battery_watts" not in out
    assert "charge_state_name" not in out


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("0", "off"),
        ("2", "fault"),
        ("3", "bulk"),
        ("4", "absorption"),
        ("5", "float"),
        ("7", "equalize"),
        ("247", "external-control"),
    ],
)
def test_charge_state_names(code: str, expected: str) -> None:
    assert normalize({"CS": code})["charge_state_name"] == expected


def test_charge_state_unknown_surfaces() -> None:
    assert normalize({"CS": "99"})["charge_state_name"] == "unknown-99"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("0", "off"),
        ("1", "voltage-or-current-limited"),
        ("2", "mpp-tracker-active"),
    ],
)
def test_mppt_state_names(code: str, expected: str) -> None:
    assert normalize({"MPPT": code})["mppt_state_name"] == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("2", "battery-voltage-too-high"),
        ("17", "charger-temperature-too-high"),
        ("18", "charger-over-current"),
        ("19", "charger-current-reversed"),
        ("20", "bulk-time-limit-exceeded"),
        ("21", "current-sensor-issue"),
        ("33", "input-voltage-too-high"),
        ("34", "input-current-too-high"),
        ("38", "input-shutdown-battery-voltage"),
        ("39", "input-shutdown-current-flow"),
    ],
)
def test_error_codes_decoded(code: str, expected: str) -> None:
    assert normalize({"ERR": code})["error_name"] == expected


def test_error_zero_is_null() -> None:
    assert normalize({"ERR": "0"})["error_name"] is None


def test_error_unknown_surfaces() -> None:
    assert normalize({"ERR": "123"})["error_name"] == "unknown-123"


def test_malformed_int_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="doghouse.normalize"):
        out = normalize({"V": "12800", "I": "not-a-number"})
    assert out["V"] == pytest.approx(12.8)
    assert "I" not in out
    assert "P_battery_watts" not in out
    assert any("I" in record.message for record in caplog.records)


def test_empty_input_is_empty_output() -> None:
    assert normalize({}) == {}
