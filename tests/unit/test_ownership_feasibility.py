"""Heterogeneous fixtures for the prototype ownership-feasibility gate.

Fixture vocabulary is deliberately varied (architecture, character, product) because the
mechanism must generalize across shot families; only fixtures may know shots, never the
checker. The transcription fixture reproduces the two defects published in bundle
a5692e9f (run 20260823T110844Z-6281c8) and shows both are rejected from roles,
dependencies, moments, and boundaries alone."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.evaluation.ownership_feasibility import check


def _chain(n: int, roles: dict[str, list[str]] | None = None) -> list[dict]:
    layers = []
    for i in range(1, n + 1):
        lid = str(i)
        layers.append({
            "id": lid,
            "judge_frames": [i * 10],
            "reserved_roles": (roles or {}).get(lid, [f"system_{lid}.*"]),
            "depends_on": [] if i == 1 else [str(i - 1)],
        })
    return layers


# --- transcription of the published-bundle defects (shot vocabulary allowed here) ----


def _published_bundle_shape() -> dict:
    layers = _chain(8)
    layers[0]["judge_frames"] = [1, 36]
    layers[1]["reserved_roles"] = ["foundry_shell.*", "gantry.*"]
    layers[5]["reserved_roles"] = ["debris_pieces.fracture.*"]
    layers[6]["reserved_roles"] = ["debris_pieces.reassembly.*"]
    layers[7]["judge_frames"] = [240]
    return {
        "layers": layers,
        "requirements": [
            # R63-shaped: silhouette readability at four frames, parked on the final
            # layer, which judges only f240.
            {"id": "silhouette-multiframe", "producer": "8",
             "verify_at": {"kind": "layer", "layer": "8"},
             "repair_routes": ["8"], "moments": [132, 150, 204, 240]},
            # Fracture-shaped: a late layer must mutate architecture reserved upstream.
            {"id": "architecture-mutation", "producer": "6",
             "verify_at": {"kind": "layer", "layer": "6"},
             "repair_routes": ["6"],
             "implicated_roles": ["foundry_shell.*"]},
        ],
        "interfaces": [],
    }


def test_published_bundle_defects_are_both_rejected() -> None:
    findings, _ = check(_published_bundle_shape())
    checks = {f.check for f in findings}
    assert "moment-observability" in checks
    assert "handoff-declaration" in checks
    observability = next(f for f in findings if f.check == "moment-observability")
    assert "132" in observability.detail and "204" in observability.detail


def test_published_bundle_shape_passes_once_ownership_is_executable() -> None:
    record = _published_bundle_shape()
    record["requirements"][0]["verify_at"] = {"kind": "acceptance"}
    record["requirements"][0]["repair_routes"] = ["4", "5"]
    record["requirements"][1]["repair_routes"] = ["2", "6"]
    record["interfaces"] = [{
        "id": "architecture.mutable", "producer": "2", "consumers": ["6", "7"],
        "mode": "ordered_mutation_handoff",
    }]
    findings, obligations = check(record)
    assert findings == []
    assert any(o.kind == "identity_preservation" for o in obligations)


# --- heterogeneous archetypes -------------------------------------------------------


def test_architecture_created_early_animated_later_needs_declared_handoff() -> None:
    record = {
        "layers": _chain(3, {"1": ["structure.*"], "2": ["dressing.*"], "3": ["motion.*"]}),
        "requirements": [{
            "id": "animate-structure", "producer": "3",
            "verify_at": {"kind": "layer", "layer": "3"},
            "repair_routes": ["3"], "implicated_roles": ["structure.towers.*"],
        }],
        "interfaces": [],
    }
    findings, _ = check(record)
    assert any(f.check == "handoff-declaration" for f in findings)

    record["interfaces"] = [{
        "id": "structure.mutable", "producer": "1", "consumers": ["3"],
        "mode": "ordered_mutation_handoff",
    }]
    findings, _ = check(record)
    assert not any(f.check == "handoff-declaration" for f in findings)


def test_character_prop_handoff_between_units() -> None:
    record = {
        "layers": _chain(2, {"1": ["character.hero.*"], "2": ["performance.*"]}),
        "requirements": [{
            "id": "pose-the-hero", "producer": "2",
            "verify_at": {"kind": "layer", "layer": "2"},
            "repair_routes": ["1", "2"], "implicated_roles": ["character.hero.rig"],
        }],
        "interfaces": [{
            "id": "hero.mutable", "producer": "1", "consumers": ["2"],
            "mode": "ordered_mutation_handoff",
        }],
    }
    findings, _ = check(record)
    assert findings == []


def test_static_product_shot_requires_no_handoff() -> None:
    record = {
        "layers": [{"id": "1", "judge_frames": [1], "reserved_roles": ["product.*"],
                    "depends_on": []}],
        "requirements": [{
            "id": "label-legible", "producer": "1",
            "verify_at": {"kind": "layer", "layer": "1"},
            "repair_routes": ["1"], "moments": [1],
            "implicated_roles": ["product.label"],
        }],
        "interfaces": [],
    }
    findings, obligations = check(record)
    assert findings == []
    assert obligations == []


def test_multiframe_acceptance_rule_cannot_be_parked_on_one_layer() -> None:
    record = {
        "layers": _chain(4),
        "requirements": [{
            "id": "arc-holds-throughout", "producer": "4",
            "verify_at": {"kind": "layer", "layer": "4"},
            "repair_routes": ["4"], "moments": [10, 20, 30, 40],
        }],
        "interfaces": [],
    }
    findings, _ = check(record)
    assert any(f.check == "moment-observability" for f in findings)

    record["requirements"][0]["verify_at"] = {"kind": "acceptance"}
    findings, _ = check(record)
    assert findings == []


def test_singleframe_local_requirement_stays_layer_owned() -> None:
    record = {
        "layers": _chain(2),
        "requirements": [{
            "id": "local-fact", "producer": "2",
            "verify_at": {"kind": "layer", "layer": "2"},
            "repair_routes": ["2"], "moments": [20],
            "implicated_roles": ["system_2.dial"],
        }],
        "interfaces": [],
    }
    findings, _ = check(record)
    assert findings == []


# --- relationship rules -------------------------------------------------------------


def test_repair_route_must_reach_the_implicated_role() -> None:
    record = {
        "layers": _chain(2),
        "requirements": [{
            "id": "unreachable-repair", "producer": "2",
            "verify_at": {"kind": "layer", "layer": "2"},
            "repair_routes": ["1"],  # layer 1 reserves system_1.*, not system_2.*
            "implicated_roles": ["system_2.dial"],
        }],
        "interfaces": [],
    }
    findings, _ = check(record)
    assert any(f.check == "repair-reachability" for f in findings)


def test_handoff_must_follow_the_dag() -> None:
    layers = _chain(3, {"1": ["set.*"], "2": ["light.*"], "3": ["move.*"]})
    layers[2]["depends_on"] = []  # consumer no longer depends on the producer
    record = {
        "layers": layers,
        "requirements": [],
        "interfaces": [{
            "id": "set.mutable", "producer": "1", "consumers": ["3"],
            "mode": "ordered_mutation_handoff",
        }],
    }
    findings, _ = check(record)
    assert any(
        f.check == "handoff-dag" and "does not depend on producer" in f.detail
        for f in findings
    )


def test_declared_mutation_order_must_match_the_dag() -> None:
    record = {
        "layers": _chain(3, {"1": ["set.*"], "2": ["strike.*"], "3": ["restore.*"]}),
        "requirements": [],
        "interfaces": [{
            "id": "set.mutable", "producer": "1", "consumers": ["3", "2"],  # reversed
            "mode": "ordered_mutation_handoff",
        }],
    }
    findings, _ = check(record)
    assert any(
        f.check == "handoff-dag" and "declared mutation order" in f.detail
        for f in findings
    )


def test_undeclared_reservation_overlap_remains_forbidden() -> None:
    record = {
        "layers": [
            {"id": "1", "judge_frames": [1], "reserved_roles": ["shared.*"],
             "depends_on": []},
            {"id": "2", "judge_frames": [2], "reserved_roles": ["shared.thing"],
             "depends_on": ["1"]},
        ],
        "requirements": [],
        "interfaces": [{
            "id": "shared.mutable", "producer": "1", "consumers": ["2"],
            "mode": "ordered_mutation_handoff",
        }],
    }
    findings, _ = check(record)
    assert any(f.check == "reservation-overlap" for f in findings)


def test_checker_source_contains_no_shot_vocabulary() -> None:
    """The mechanism must reason only over roles, dependencies, moments, and
    boundaries. Fixtures may know shots; the checker may not."""
    source = (
        Path(__file__).resolve().parents[2]
        / "src" / "vfx_harness" / "evaluation" / "ownership_feasibility.py"
    ).read_text(encoding="utf-8").lower()
    for word in ("iris", "fracture", "gantry", "foundry", "reactor", "blade",
                 "debris", "ignition", "camera_spine", "f36", "240"):
        assert word not in source, f"shot vocabulary {word!r} leaked into core checker"
