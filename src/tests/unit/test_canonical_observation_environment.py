"""Canonical qualitative-observation environment boundary without Blender."""

from __future__ import annotations

import pytest

from vfx_harness.blender.observation_environment import (
    SCHEMA,
    canonical_observation_environment,
    validate_observation_request,
)
from vfx_harness.blender.session import BlenderSession


def _snapshot() -> dict:
    return {
        "schema": SCHEMA,
        "frame": 40,
        "observation_medium": "eevee",
        "render": {"engine": "BLENDER_EEVEE_NEXT", "resolution": [1280, 720]},
        "subjects": [{"role": "hall.mass", "bbox_world": {"min": [0, 0, 0]}}],
    }


def test_canonical_observation_environment_is_order_independent_and_hashable() -> None:
    first = canonical_observation_environment(_snapshot())
    second = canonical_observation_environment({
        "subjects": [{"bbox_world": {"min": [0, 0, 0]}, "role": "hall.mass"}],
        "render": {"resolution": [1280, 720], "engine": "BLENDER_EEVEE_NEXT"},
        "observation_medium": "eevee",
        "frame": 40,
        "schema": SCHEMA,
    })

    assert first["digest"] == second["digest"]
    assert first["canonical_json"] == second["canonical_json"]
    assert first["snapshot"]["frame"] == 40


@pytest.mark.parametrize(
    ("frame", "roles", "medium", "message"),
    [
        (True, ("hall",), "eevee", "frame"),
        (40, (), "eevee", "subject_roles"),
        (40, ("hall, tower",), "eevee", "one semantic selector"),
        (40, ("hall", "hall"), "eevee", "must not repeat"),
        (40, ("hall",), "beauty", "medium"),
    ],
)
def test_canonical_observation_request_rejects_malformed_authority(
    frame, roles, medium, message
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_observation_request(frame, roles, medium, ("mesh",))


@pytest.mark.parametrize(
    ("families", "message"),
    [
        ((), "carrier_families"),
        (("mesh", "mesh"), "must not repeat"),
        (("light",), "only mesh, volume, compositor"),
    ],
)
def test_canonical_observation_request_rejects_malformed_carrier_authority(
    families, message
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_observation_request(40, ("hall",), "eevee", families)


def test_canonical_observation_request_canonicalizes_carrier_family_order() -> None:
    assert validate_observation_request(
        40, ("hall",), "eevee", ("volume", "mesh")
    ) == (40, ("hall",), "eevee", ("mesh", "volume"))


def test_session_probe_validates_then_uses_typed_worker_operation(monkeypatch) -> None:
    session = object.__new__(BlenderSession)
    calls = []

    def call(command: str, **args) -> dict:
        calls.append((command, args))
        return {"digest": "d" * 64}

    monkeypatch.setattr(session, "call", call)

    result = session.canonical_observation_environment(
        frame=40,
        subject_roles=["hall", "tower.*"],
        observation_medium="workbench_solid",
        carrier_families=["compositor", "mesh"],
    )

    assert result == {"digest": "d" * 64}
    assert calls == [(
        "observation_environment",
        {
            "frame": 40,
            "subject_roles": ["hall", "tower.*"],
            "observation_medium": "workbench_solid",
            "carrier_families": ["compositor", "mesh"],
        },
    )]
