"""Normalize a raw VE.Direct frame into SI-scaled fields plus derived values.

The raw frame from ``vedirect-m8`` is a ``str -> str`` mapping using the
integer scaling Victron uses on the wire (e.g. ``V`` is millivolts,
``H19`` is 10 Wh units). This module converts to SI and derives a few
fields the rest of the logger relies on.

Conventions from the project spec:

- ``V``, ``I``, ``VPV``, ``IL`` are scaled from the raw int by ``/ 1000``
  (``I`` is signed; negative values mean discharge).
- ``H19``, ``H20``, ``H22`` are scaled by ``/ 100`` to yield kWh, and are
  emitted under keys ``H19_kwh``, ``H20_kwh``, ``H22_kwh``.
- ``PPV``, ``H21``, ``H23`` are already watts; they are emitted as floats
  with no scaling.
- Derived: ``P_battery_watts = V * I``,
  ``charge_state_name`` (from ``CS``), ``mppt_state_name`` (from
  ``MPPT``), ``error_name`` (from ``ERR``; ``None`` when ``ERR == 0``).

Unknown fields in the input are ignored. Malformed integer values are
logged and the corresponding output fields are omitted rather than
poisoning the whole frame.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

_LOG = logging.getLogger(__name__)

NormalizedFrame = dict[str, float | str | list[str] | None]

_MILLI_FIELDS: Final[tuple[str, ...]] = ("V", "I", "VPV", "IL")
_CENTI_KWH_FIELDS: Final[tuple[str, ...]] = ("H19", "H20", "H22")
_WATT_PASSTHROUGH_FIELDS: Final[tuple[str, ...]] = ("PPV", "H21", "H23")

_CHARGE_STATES: Final[dict[int, str]] = {
    0: "off",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    7: "equalize",
    247: "external-control",
}

_MPPT_STATES: Final[dict[int, str]] = {
    0: "off",
    1: "voltage-or-current-limited",
    2: "mpp-tracker-active",
}

# Subset of VE.Direct error codes required by the project spec. Any code
# not listed here surfaces as ``unknown-<n>`` so unexpected faults are
# visible downstream instead of silently mapping to "no error".
_ERROR_CODES: Final[dict[int, str]] = {
    2: "battery-voltage-too-high",
    17: "charger-temperature-too-high",
    18: "charger-over-current",
    19: "charger-current-reversed",
    20: "bulk-time-limit-exceeded",
    21: "current-sensor-issue",
    33: "input-voltage-too-high",
    34: "input-current-too-high",
    38: "input-shutdown-battery-voltage",
    39: "input-shutdown-current-flow",
}

# --- Inverter (Phoenix) field maps -------------------------------------------
# VERIFIED 2026-06-12 against the official Victron "VE.Direct Protocol" PDF
# v3.33 (victronenergy.com/upload/documents/VE.Direct-Protocol-3.33.pdf). Each
# table below was checked field-by-field against that document; see the
# per-table notes for the section verified and the per-bit pinning tests in
# tests/test_normalize_inverter.py. (vedirect-m8 v1.3.4, the parser we use, is
# pure transport — it returns raw str->str and embeds NO decode tables — so the
# PDF, not the library, is the authority.)
#
# Defence-in-depth regardless of the above: the RAW MODE/CS/AR/WARN/OR values
# are stored verbatim in the raw frame (raw_json), and `_decode_bitmask` keeps
# unmapped bits as `unknown-0x..`, so the underlying signal is never lost even
# if a name here is later found wrong. Re-verify against the PDF when adding a
# bit/code and update the date above.

# MODE (device mode), PDF "MODE" table: 1=CHARGER 2=INVERTER 4=OFF 5=ECO
# 253=HIBERNATE. (2 named "inverter_on" here for clarity; value is authoritative.)
_INVERTER_MODES: Final[dict[int, str]] = {
    1: "charger",
    2: "inverter_on",
    4: "off",
    5: "eco",
    253: "hibernate",
}

# CS (state of operation), PDF "CS" table — inverter-relevant values only:
# 0=Off 1=Low power 2=Fault 9=Inverting. Charger states (3=Bulk, 4=Absorption,
# 5=Float, ...) are intentionally omitted for an inverter source; they would
# surface as `unknown-<n>` if ever seen, not be silently misread.
_INVERTER_STATES: Final[dict[int, str]] = {
    0: "off",
    1: "low_power",
    2: "fault",
    9: "inverting",
}

# AR (alarm reason) and WARN (warning reason) share this bit table. PDF "AR"
# table (values sent in decimal, summed); WARN: spec states "the bit definition
# is the same as for AR". Verified all 14 bits, incl. the DC vs AC-out trap
# (0x0001 Low Voltage is battery DC; 0x0400 Low V AC out is the AC output).
_ALARM_REASONS: Final[dict[int, str]] = {
    0x0001: "low_voltage",
    0x0002: "high_voltage",
    0x0004: "low_soc",
    0x0008: "low_starter_voltage",
    0x0010: "high_starter_voltage",
    0x0020: "low_temperature",
    0x0040: "high_temperature",
    0x0080: "mid_voltage",
    0x0100: "overload",
    0x0200: "dc_ripple",
    0x0400: "low_v_ac_out",
    0x0800: "high_v_ac_out",
    0x1000: "short_circuit",
    0x2000: "bms_lockout",
}

# OR (off reason), a 32-bit hex bitmask. PDF "OR" table, all 9 bits verified.
# Bit 0 (0x00000001 = no_input_power) is field-confirmed: the MPPT's real
# nighttime frame reports OR 0x00000001 (no solar input).
_OFF_REASONS: Final[dict[int, str]] = {
    0x00000001: "no_input_power",
    0x00000002: "switched_off_power_switch",
    0x00000004: "switched_off_register",
    0x00000008: "remote_input",
    0x00000010: "protection_active",
    0x00000020: "paygo",
    0x00000040: "bms",
    0x00000080: "engine_shutdown",
    0x00000100: "analysing_input_voltage",
}


def normalize_mppt(raw: dict[str, str]) -> NormalizedFrame:
    """Return a derived view of a raw VE.Direct frame.

    Args:
        raw: Key/value pairs exactly as produced by ``vedirect-m8``. All
            values are the string forms of Victron's wire integers (or
            short labels like ``ON``/``OFF`` for fields we ignore here).

    Returns:
        A new dict containing scaled SI fields, derived names, and
        ``P_battery_watts``. Fields absent or unparseable in ``raw`` are
        omitted. The input is not mutated.
    """
    out: NormalizedFrame = {}
    scaled: dict[str, float] = {}

    for key in _MILLI_FIELDS:
        value = _parse_int(raw.get(key), key)
        if value is not None:
            scaled[key] = value / 1000.0
            out[key] = scaled[key]

    for key in _CENTI_KWH_FIELDS:
        value = _parse_int(raw.get(key), key)
        if value is not None:
            out[f"{key}_kwh"] = value / 100.0

    for key in _WATT_PASSTHROUGH_FIELDS:
        value = _parse_int(raw.get(key), key)
        if value is not None:
            out[key] = float(value)

    volts = scaled.get("V")
    amps = scaled.get("I")
    if volts is not None and amps is not None:
        out["P_battery_watts"] = round(volts * amps, 3)

    cs = _parse_int(raw.get("CS"), "CS")
    if cs is not None:
        out["charge_state_name"] = _CHARGE_STATES.get(cs, f"unknown-{cs}")

    mppt = _parse_int(raw.get("MPPT"), "MPPT")
    if mppt is not None:
        out["mppt_state_name"] = _MPPT_STATES.get(mppt, f"unknown-{mppt}")

    err = _parse_int(raw.get("ERR"), "ERR")
    if err is not None:
        out["error_name"] = None if err == 0 else _ERROR_CODES.get(err, f"unknown-{err}")

    return out


def normalize_inverter(raw: dict[str, str]) -> NormalizedFrame:
    """Return a derived view of a raw VE.Direct INVERTER (Phoenix) frame.

    Scales the AC-output + battery fields to SI, maps ``MODE``/``CS`` to readable
    names, and decodes the ``AR``/``WARN``/``OR`` bitmasks to sorted name lists
    (unmapped bits preserved as ``unknown-0x..``). The original codes live in the
    stored raw frame, so nothing is lost. Absent/unparseable fields are omitted;
    the input is not mutated.
    """
    out: NormalizedFrame = {}

    v = _parse_int(raw.get("V"), "V")
    if v is not None:
        out["V"] = v / 1000.0  # mV -> V (battery)

    ac_v = _parse_int(raw.get("AC_OUT_V"), "AC_OUT_V")
    if ac_v is not None:
        out["AC_OUT_V"] = ac_v / 100.0  # 0.01 V units -> V

    ac_i = _parse_int(raw.get("AC_OUT_I"), "AC_OUT_I")
    if ac_i is not None:
        out["AC_OUT_I"] = ac_i / 10.0  # 0.1 A units -> A

    ac_s = _parse_int(raw.get("AC_OUT_S"), "AC_OUT_S")
    if ac_s is not None:
        out["AC_OUT_S"] = float(ac_s)  # VA (apparent power)

    mode = _parse_int(raw.get("MODE"), "MODE")
    if mode is not None:
        out["mode_name"] = _INVERTER_MODES.get(mode, f"unknown-{mode}")

    cs = _parse_int(raw.get("CS"), "CS")
    if cs is not None:
        out["state_name"] = _INVERTER_STATES.get(cs, f"unknown-{cs}")

    ar = _parse_int(raw.get("AR"), "AR")
    if ar is not None:
        out["alarm_reasons"] = _decode_bitmask(ar, _ALARM_REASONS)

    warn = _parse_int(raw.get("WARN"), "WARN")
    if warn is not None:
        out["warning_reasons"] = _decode_bitmask(warn, _ALARM_REASONS)

    off = _parse_hex(raw.get("OR"), "OR")
    if off is not None:
        out["off_reasons"] = _decode_bitmask(off, _OFF_REASONS)

    return out


def _parse_int(value: str | None, key: str) -> int | None:
    """Parse a VE.Direct integer field; warn and return ``None`` on garbage."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        _LOG.warning("malformed VE.Direct field %s=%r; dropping", key, value)
        return None


def _parse_hex(value: str | None, key: str) -> int | None:
    """Parse a VE.Direct hex bitmask field (e.g. ``0x00000010``); None on garbage."""
    if value is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        _LOG.warning("malformed VE.Direct hex field %s=%r; dropping", key, value)
        return None


def _decode_bitmask(value: int, table: dict[int, str]) -> list[str]:
    """Decode a bitmask to a sorted list of set-bit names.

    Bits present in ``table`` map to their names; any remaining (unmapped) bits
    are preserved as ``unknown-0x<hex>`` rather than dropped, so an incomplete
    table never silently loses a signal. ``0`` yields ``[]`` (nothing set).
    """
    names: list[str] = []
    remaining = value
    for bit, name in table.items():
        if value & bit:
            names.append(name)
            remaining &= ~bit
    if remaining:
        names.append(f"unknown-0x{remaining:x}")
    return sorted(names)


Normalizer = Callable[[dict[str, str]], NormalizedFrame]

_NORMALIZERS: Final[dict[str, Normalizer]] = {
    "solar_mppt": normalize_mppt,
    "inverter": normalize_inverter,
}


def get_normalizer(source_type: str) -> Normalizer:
    """Return the normalizer for ``source_type`` — fail-fast on unknown.

    Resolved once at startup so a misconfigured ``SOURCE_TYPE`` crashes the
    process immediately with a clear message, rather than silently
    mis-normalizing every frame.
    """
    try:
        return _NORMALIZERS[source_type]
    except KeyError:
        known = ", ".join(sorted(_NORMALIZERS))
        raise ValueError(
            f"unknown SOURCE_TYPE {source_type!r}; known source types: {known}"
        ) from None
