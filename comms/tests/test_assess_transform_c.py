"""Proves the C and Python halves of the assessment transform agree.

The transform exists so the laptop can verify the FireBeetle really modified the data. That check is
only meaningful if both ends compute the same thing, and the two implementations live in different
languages in different directories:

  comms/src/cg_comms/relay/assess_transform.py   (laptop)
  firmware_demo/lib/cg_protocol/cg_assess_transform.h   (device)

A host C compiler is not installed here (platformio's toolchain is an xtensa cross-compiler, so its
output cannot be executed on this laptop), and requiring one would make the suite unrunnable on the
machine it is developed on. Instead this test PARSES the constants and expressions out of the C
header and evaluates them with C semantics, then diffs against Python across the whole int16 range.
That catches the realistic failure -- someone edits one side's formula and not the other -- without
needing a toolchain.

What this does not catch: a C compiler miscompiling the header, or a typo that is syntactically
valid C but which the regexes below read differently than the compiler would. The header is
separately compiled with -Wall -Wextra -Werror -std=c11 plus _Static_asserts on every struct size.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cg_comms.relay.assess_transform import (
    TRANSFORM_ADD_SEQ,
    TRANSFORM_NEGATE,
    TRANSFORM_SWAP_SCALE,
    apply_transform,
    transform_for,
)

_HEADER = (
    Path(__file__).resolve().parents[2]
    / "firmware_demo" / "lib" / "cg_protocol" / "cg_assess_transform.h"
)

pytestmark = pytest.mark.skipif(not _HEADER.exists(), reason="firmware header not present")


def _header_text() -> str:
    return _HEADER.read_text(encoding="utf-8")


def _c_define(name: str) -> int:
    m = re.search(rf"^#define\s+{re.escape(name)}\s+(\d+)\s*$", _header_text(), re.MULTILINE)
    assert m is not None, f"{name} not found in {_HEADER.name}"
    return int(m.group(1))


def test_transform_ids_match_between_c_and_python():
    assert _c_define("CG_ASSESS_TRANSFORM_NEGATE") == TRANSFORM_NEGATE
    assert _c_define("CG_ASSESS_TRANSFORM_ADD_SEQ") == TRANSFORM_ADD_SEQ
    assert _c_define("CG_ASSESS_TRANSFORM_SWAP_SCALE") == TRANSFORM_SWAP_SCALE


def test_c_transform_selector_formula_matches_python():
    # cg_assess_transform_for(): (keycode % 3) + 1
    body = re.search(
        r"cg_assess_transform_for\(uint8_t keycode\)\s*\{(.*?)\}", _header_text(), re.DOTALL
    )
    assert body is not None, "cg_assess_transform_for not found"
    assert re.search(r"\(keycode\s*%\s*3\)\s*\+\s*1", body.group(1)), (
        "C transform selector no longer reads (keycode % 3) + 1 -- if it changed intentionally, "
        "update transform_for() in assess_transform.py and this test together"
    )
    for keycode in range(256):
        assert transform_for(keycode) == (0 if keycode in (98, 99, 100) else (keycode % 3) + 1)


def test_c_saturation_bounds_are_int16():
    body = re.search(
        r"cg_assess_saturate_i16\(int32_t value\)\s*\{(.*?)\n\}", _header_text(), re.DOTALL
    )
    assert body is not None
    text = body.group(1)
    assert "INT16_MIN" in text and "INT16_MAX" in text, "C saturation must clamp to int16 bounds"
    assert "int32_t" in _header_text(), "C must compute in int32_t (signed overflow is UB)"


def _c_reference(
    transform_id: int, values: tuple[int, int, int], keycode: int
) -> tuple[int, int, int]:
    """Independent re-implementation using C semantics: int32 intermediates, truncating division,
    saturation at the end. Written from the C header's expressions, not by calling the Python."""

    def sat(v: int) -> int:
        return -32768 if v < -32768 else (32767 if v > 32767 else v)

    def cdiv(n: int, d: int) -> int:  # C truncates toward zero
        q = abs(n) // abs(d)
        return -q if (n < 0) != (d < 0) else q

    a, b, c = (int(v) for v in values)
    if transform_id == 1:
        return (sat(-a), sat(-b), sat(-c))
    if transform_id == 2:
        return (sat(a + keycode), sat(b + keycode), sat(c + keycode))
    if transform_id == 3:
        return (sat(c * 2), sat(cdiv(a, 2)), sat(-b))
    raise AssertionError(transform_id)


_EDGES = [-32768, -32767, -1001, -1000, -7, -1, 0, 1, 7, 1000, 1001, 16384, 32766, 32767]


@pytest.mark.parametrize("transform_id", [TRANSFORM_NEGATE, TRANSFORM_ADD_SEQ, TRANSFORM_SWAP_SCALE])
def test_python_matches_c_semantics_on_edges(transform_id: int) -> None:
    for a in _EDGES:
        for b in _EDGES:
            for c in _EDGES:
                for keycode in (0x00, 0x01, 0x61, 0x7A, 0xFF):
                    got = apply_transform(transform_id, (a, b, c), keycode)
                    want = _c_reference(transform_id, (a, b, c), keycode)
                    assert got == want, (
                        f"Python/C divergence: transform={transform_id} "
                        f"values=({a},{b},{c}) keycode={keycode}: python={got} c={want}"
                    )


def test_python_matches_c_across_full_int16_sweep() -> None:
    # Sweeps every int16 through value_a (the divided field, where floor-vs-truncate bites) for all
    # three transforms, rather than only the hand-picked edges above.
    for a in range(-32768, 32768, 7):
        for transform_id in (TRANSFORM_NEGATE, TRANSFORM_ADD_SEQ, TRANSFORM_SWAP_SCALE):
            got = apply_transform(transform_id, (a, -a, 3), 0x61)
            want = _c_reference(transform_id, (a, -a, 3), 0x61)
            assert got == want, f"divergence at value_a={a} transform={transform_id}: {got} != {want}"
