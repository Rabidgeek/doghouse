"""Unit tests for the inverter normalizer + the source_type dispatch."""

from __future__ import annotations

import pytest

from doghouse.normalize import (
    get_normalizer,
    normalize_inverter,
    normalize_mppt,
)

# The real Phoenix inverter frame observed on the wire (healthy, idle).
_FRAME = {
    "PID": "0xA259",
    "FW": "0127",
    "SER#": "HQ2136A4M7A",
    "MODE": "2",
    "CS": "9",
    "AC_OUT_V": "12063",
    "AC_OUT_I": "0",
    "AC_OUT_S": "9",
    "V": "13213",
    "AR": "0",
    "WARN": "0",
    "OR": "0x00000000",
}


def test_golden_inverter_frame() -> None:
    out = normalize_inverter(_FRAME)
    assert out["V"] == pytest.approx(13.213)        # mV -> V
    assert out["AC_OUT_V"] == pytest.approx(120.63)  # 0.01 V units -> V
    assert out["AC_OUT_I"] == 0.0                    # 0.1 A units -> A
    assert out["AC_OUT_S"] == 9.0                    # VA
    assert out["mode_name"] == "inverter_on"
    assert out["state_name"] == "inverting"
    assert out["alarm_reasons"] == []
    assert out["warning_reasons"] == []
    assert out["off_reasons"] == []
    # PID/FW/SER# are identity metadata — kept in the raw frame, not derived
    # (consistent with normalize_mppt, which ignores unknown fields).
    assert "PID" not in out
    assert "SER#" not in out


def test_inverter_scaling() -> None:
    out = normalize_inverter(
        {"AC_OUT_V": "12000", "AC_OUT_I": "15", "AC_OUT_S": "180", "V": "12500"}
    )
    assert out["AC_OUT_V"] == 120.0
    assert out["AC_OUT_I"] == 1.5
    assert out["AC_OUT_S"] == 180.0
    assert out["V"] == 12.5


def test_inverter_mode_and_state_maps() -> None:
    assert normalize_inverter({"MODE": "4"})["mode_name"] == "off"
    assert normalize_inverter({"MODE": "5"})["mode_name"] == "eco"
    assert normalize_inverter({"CS": "0"})["state_name"] == "off"
    assert normalize_inverter({"CS": "2"})["state_name"] == "fault"


def test_inverter_unknown_mode_and_state_surface() -> None:
    assert normalize_inverter({"MODE": "99"})["mode_name"] == "unknown-99"
    assert normalize_inverter({"CS": "42"})["state_name"] == "unknown-42"


@pytest.mark.parametrize(
    ("ar", "expected"),
    [
        ("0", []),                                  # nothing set
        ("1", ["low_voltage"]),                     # single bit
        ("256", ["overload"]),                      # single high-ish bit
        ("3", ["high_voltage", "low_voltage"]),     # 0x1|0x2, sorted
        ("260", ["low_soc", "overload"]),           # 0x4|0x100, sorted
    ],
)
def test_alarm_bitmask_decode(ar: str, expected: list[str]) -> None:
    assert normalize_inverter({"AR": ar})["alarm_reasons"] == expected


def test_warn_uses_the_same_table() -> None:
    assert normalize_inverter({"WARN": "2"})["warning_reasons"] == ["high_voltage"]


def test_unknown_high_bit_preserved_numerically() -> None:
    # 0x8000 is above the known table; it must survive as unknown-0x8000, never
    # be silently dropped (the bit is a real signal we just don't have a name for).
    out = normalize_inverter({"AR": "32769"})  # 0x8000 | 0x0001
    assert out["alarm_reasons"] == ["low_voltage", "unknown-0x8000"]


def test_off_reason_hex_decode() -> None:
    assert normalize_inverter({"OR": "0x00000000"})["off_reasons"] == []
    assert normalize_inverter({"OR": "0x00000001"})["off_reasons"] == ["no_input_power"]
    # 0x01 | 0x10 → no_input_power + protection_active, sorted.
    assert normalize_inverter({"OR": "0x00000011"})["off_reasons"] == [
        "no_input_power",
        "protection_active",
    ]


def test_malformed_fields_dropped_not_poisoning() -> None:
    out = normalize_inverter({"AC_OUT_V": "garbage", "OR": "nothex", "V": "13200"})
    assert "AC_OUT_V" not in out
    assert "off_reasons" not in out
    assert out["V"] == 13.2  # the good field still comes through


def test_input_not_mutated() -> None:
    frame = dict(_FRAME)
    normalize_inverter(frame)
    assert frame == _FRAME


# --- dispatch ----------------------------------------------------------------


def test_dispatch_routes_each_source_type() -> None:
    assert get_normalizer("solar_mppt") is normalize_mppt
    assert get_normalizer("inverter") is normalize_inverter


def test_dispatch_unknown_source_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown SOURCE_TYPE"):
        get_normalizer("bogus")


# --- per-bit / per-code pinning ----------------------------------------------
# Each case below pins a single verified position from the Victron VE.Direct
# Protocol PDF v3.33 to its exact name, so a transcription slip on any one bit
# (not just a broken round-trip) fails a test. Verified 2026-06-12.


@pytest.mark.parametrize(
    ("decimal", "name"),
    [
        (1, "low_voltage"),
        (2, "high_voltage"),
        (4, "low_soc"),
        (8, "low_starter_voltage"),
        (16, "high_starter_voltage"),
        (32, "low_temperature"),
        (64, "high_temperature"),
        (128, "mid_voltage"),
        (256, "overload"),
        (512, "dc_ripple"),
        (1024, "low_v_ac_out"),  # AC output — NOT battery 'low_voltage' (bit 0)
        (2048, "high_v_ac_out"),
        (4096, "short_circuit"),
        (8192, "bms_lockout"),
    ],
)
def test_ar_each_bit_pinned(decimal: int, name: str) -> None:
    # AR is sent in decimal per the spec; one bit set -> exactly that one name.
    assert normalize_inverter({"AR": str(decimal)})["alarm_reasons"] == [name]


@pytest.mark.parametrize(
    ("hexmask", "name"),
    [
        ("0x00000001", "no_input_power"),  # field-confirmed by MPPT nighttime OR
        ("0x00000002", "switched_off_power_switch"),
        ("0x00000004", "switched_off_register"),
        ("0x00000008", "remote_input"),
        ("0x00000010", "protection_active"),
        ("0x00000020", "paygo"),
        ("0x00000040", "bms"),
        ("0x00000080", "engine_shutdown"),
        ("0x00000100", "analysing_input_voltage"),
    ],
)
def test_or_each_bit_pinned(hexmask: str, name: str) -> None:
    assert normalize_inverter({"OR": hexmask})["off_reasons"] == [name]


@pytest.mark.parametrize(
    ("mode", "name"),
    [("1", "charger"), ("2", "inverter_on"), ("4", "off"), ("5", "eco"), ("253", "hibernate")],
)
def test_mode_each_code_pinned(mode: str, name: str) -> None:
    assert normalize_inverter({"MODE": mode})["mode_name"] == name


@pytest.mark.parametrize(
    ("cs", "name"),
    [("0", "off"), ("1", "low_power"), ("2", "fault"), ("9", "inverting")],
)
def test_cs_each_code_pinned(cs: str, name: str) -> None:
    assert normalize_inverter({"CS": cs})["state_name"] == name


def test_warn_bit_definition_matches_ar() -> None:
    # The spec states WARN's bit definition is identical to AR; this guards the
    # two against drifting apart (they share _ALARM_REASONS today).
    assert (
        normalize_inverter({"WARN": "1024"})["warning_reasons"]
        == normalize_inverter({"AR": "1024"})["alarm_reasons"]
        == ["low_v_ac_out"]
    )
