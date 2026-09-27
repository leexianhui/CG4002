"""Live-editable keyboard IMU data. Save this file, then press b/c/d: no restart needed.

EDIT ONLY the three values tuples in DUMMY_VARIANTS below (signed int16 literals).
Mapping is fixed: b -> B, c -> C, d -> D. To demonstrate B := C, copy C's tuple
onto B's row, save, then press b. Keep the names and IDs unchanged.

Each press safely parses and validates the saved table before sending. Invalid edits
reject the press, never send stale data. In-flight requests keep their original snapshot.
These are dominant acceleration axes in the real 42-byte IMU body; the common gyro,
offhand and torso fields remain fixed in assess_echo.py. GPIO data is independent.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

I16_MIN = -32768
I16_MAX = 32767


@dataclass(frozen=True)
class DummyVariant:
    """One selectable dummy payload. `kind` goes on the wire as ASSESS_ECHO_REQ.payload_kind and is
    echoed back untouched by the device, so a response can be attributed to its variant even if the
    transformed values are unrecognisable."""

    kind: int
    name: str
    values: tuple[int, int, int]
    note: str = ""


# --- EDIT ME DURING THE DEMO -------------------------------------------------------------------
DUMMY_VARIANTS: tuple[DummyVariant, ...] = (
    DummyVariant(1, "B", (2048, -1024, 512), "keyboard b: dominant acceleration raw int16"),
    DummyVariant(2, "C", (1000, -2000, 3000), "keyboard c: dominant acceleration raw int16"),
    DummyVariant(3, "D", (7, 13, 31), "keyboard d: dominant acceleration raw int16"),
)
# --- END EDIT ME -------------------------------------------------------------------------------


def validate_variants(variants: tuple[DummyVariant, ...] = DUMMY_VARIANTS) -> None:
    """Fails at import, not at the first keystroke, if the table above was edited badly."""
    if not variants:
        raise ValueError("DUMMY_VARIANTS is empty -- at least one variant is required")
    seen: dict[int, str] = {}
    for v in variants:
        if type(v.kind) is not int or not 0 <= v.kind <= 255:
            raise ValueError(f"variant {v.name!r}: kind {v.kind} outside u8 range 0..255")
        if v.kind in seen:
            raise ValueError(
                f"variant {v.name!r}: kind {v.kind} already used by {seen[v.kind]!r} -- "
                "payload_kind must be unique or responses cannot be attributed"
            )
        seen[v.kind] = v.name
        if not isinstance(v.values, tuple) or len(v.values) != 3:
            raise ValueError(f"variant {v.name!r}: expected a tuple of exactly 3 int16 values")
        for i, value in enumerate(v.values):
            if type(value) is not int or not I16_MIN <= value <= I16_MAX:
                raise ValueError(
                    f"variant {v.name!r}: values[{i}] = {value} outside int16 range "
                    f"{I16_MIN}..{I16_MAX} (ASSESS_ECHO_REQ.value_* are I16)"
                )


validate_variants()


def for_key(keycode: int, source: Path | None = None) -> DummyVariant:
    """Read the saved table on EVERY press, without executing edited Python code.

    Only literal DummyVariant(...) entries are accepted. Invalid/partially saved edits fail
    the operation before any BLE request; there is no silent fallback to stale values.
    In-flight operations retain their original validated snapshot.
    """
    key = chr(keycode)
    if key not in ("b", "c", "d"):
        raise ValueError("IMU keys are b/c/d; a selects a weapon")
    tree = ast.parse((source or Path(__file__)).read_text(encoding="utf-8-sig"))
    tables = [
        n.value
        for n in tree.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "DUMMY_VARIANTS"
    ]
    if len(tables) != 1 or not isinstance(tables[0], (ast.Tuple, ast.List)):
        raise ValueError("expected one literal DUMMY_VARIANTS table")
    variants = []
    for entry in tables[0].elts:
        if (
            not isinstance(entry, ast.Call)
            or not isinstance(entry.func, ast.Name)
            or entry.func.id != "DummyVariant"
            or entry.keywords
        ):
            raise ValueError("use literal DummyVariant(kind, name, (x, y, z), note) entries")
        variants.append(DummyVariant(*(ast.literal_eval(arg) for arg in entry.args)))
    validate_variants(tuple(variants))
    if [(v.kind, v.name) for v in variants] != [(1, "B"), (2, "C"), (3, "D")]:
        raise ValueError("keep the three identities (1, B), (2, C), (3, D); edit only values/notes")
    return variants[ord(key) - ord("b")]
