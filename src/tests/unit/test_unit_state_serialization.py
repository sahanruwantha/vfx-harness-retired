from __future__ import annotations

import pytest

from vfx_harness.orchestration.unit_state_serialization import (
    WorkUnitStateSerializationError,
    parse_work_unit_state_bytes,
    serialize_work_unit_state,
)


def test_work_unit_state_wire_format_preserves_established_ascii_escaping() -> None:
    state = {"schema": 1, "layer": "café", "units": {}}

    payload = serialize_work_unit_state(state)

    assert b"caf\\u00e9" in payload
    assert parse_work_unit_state_bytes(payload, "fixture state") == state


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":1,"units":{}}\n',
        b'{"schema": 1, "schema": 1, "units": {}}\n',
        b'{"schema": NaN, "units": {}}\n',
    ],
)
def test_work_unit_state_parser_rejects_ambiguous_or_noncanonical_bytes(
    payload: bytes,
) -> None:
    with pytest.raises(WorkUnitStateSerializationError):
        parse_work_unit_state_bytes(payload, "fixture state")
