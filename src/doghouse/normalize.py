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
from typing import Final

_LOG = logging.getLogger(__name__)

NormalizedFrame = dict[str, float | str | None]

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


def normalize(raw: dict[str, str]) -> NormalizedFrame:
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


def _parse_int(value: str | None, key: str) -> int | None:
    """Parse a VE.Direct integer field; warn and return ``None`` on garbage."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        _LOG.warning("malformed VE.Direct field %s=%r; dropping", key, value)
        return None
