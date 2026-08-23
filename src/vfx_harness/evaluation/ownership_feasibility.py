"""Prototype ownership-feasibility gate (research status — NOT wired into plan_gate).

Verifies that a sparse plan's ownership assignments are *executable* using only roles,
dependencies, moments, and boundaries. See
docs/research/ownership-feasibility-and-authoring-diet.md for the design and the
promotion criteria. Core rules here must never contain shot vocabulary; a source-scan
test enforces that.

Input is a plain-dict ownership record:

    {
      "layers": [
        {"id": "1", "judge_frames": [1, 36], "reserved_roles": ["camera.*"],
         "depends_on": []},
      ],
      "requirements": [
        {"id": "R1", "producer": "1",
         "verify_at": {"kind": "layer", "layer": "1"}    # or {"kind": "acceptance"}
                                                          # or {"kind": "harness"},
         "repair_routes": ["1"],
         "moments": [36],                # optional registration metadata
         "implicated_roles": ["camera.*"]},              # optional
      ],
      "interfaces": [
        {"id": "surface.mutable", "producer": "2", "consumers": ["6", "7"],
         "mode": "ordered_mutation_handoff"},
      ],
    }

`check(record)` returns `(findings, obligations)`. A finding blocks publication; an
obligation is registration the build phase must later discharge (for example identity
survival across a mutation handoff).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

VERIFY_KINDS = {"layer", "acceptance", "harness"}
INTERFACE_MODES = {"ordered_mutation_handoff"}


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class Obligation:
    kind: str
    subject: str
    detail: str


def _patterns_overlap(a: str, b: str) -> bool:
    return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)


def _transitive_deps(layers: dict[str, dict]) -> dict[str, set[str]]:
    closure: dict[str, set[str]] = {}

    def resolve(layer_id: str, trail: tuple[str, ...]) -> set[str]:
        if layer_id in closure:
            return closure[layer_id]
        if layer_id in trail:  # cycle: report nothing here; the DAG check owns cycles
            return set()
        direct = {str(d) for d in (layers.get(layer_id, {}).get("depends_on") or [])}
        full = set(direct)
        for dep in direct:
            full |= resolve(dep, (*trail, layer_id))
        closure[layer_id] = full
        return full

    for layer_id in layers:
        resolve(layer_id, ())
    return closure


def check(record: dict) -> tuple[list[Finding], list[Obligation]]:
    findings: list[Finding] = []
    obligations: list[Obligation] = []

    layers = {str(row.get("id")): row for row in record.get("layers") or []}
    deps = _transitive_deps(layers)
    reserved = {
        layer_id: [str(p) for p in (row.get("reserved_roles") or [])]
        for layer_id, row in layers.items()
    }

    # Undeclared reservation overlap stays forbidden; an interface is the legal route
    # for cross-namespace mutation, not a licence for overlapping reservations.
    seen: list[tuple[str, str]] = []
    for layer_id, patterns in reserved.items():
        for pattern in patterns:
            for prior_pattern, prior_layer in seen:
                if prior_layer != layer_id and _patterns_overlap(pattern, prior_pattern):
                    findings.append(Finding(
                        "reservation-overlap",
                        f"layers {prior_layer}/{layer_id}",
                        f"reserved namespaces overlap: {prior_pattern!r} vs {pattern!r}",
                    ))
            seen.append((pattern, layer_id))

    interfaces = [dict(row) for row in record.get("interfaces") or []]
    for interface in interfaces:
        iid = str(interface.get("id") or "<interface>")
        producer = str(interface.get("producer") or "")
        consumers = [str(c) for c in (interface.get("consumers") or [])]
        mode = str(interface.get("mode") or "")
        if mode not in INTERFACE_MODES:
            findings.append(Finding(
                "handoff-dag", iid, f"unknown interface mode {mode!r}"))
            continue
        if producer not in layers:
            findings.append(Finding(
                "handoff-dag", iid, f"producer {producer!r} is not a declared layer"))
            continue
        if not consumers:
            findings.append(Finding("handoff-dag", iid, "interface declares no consumers"))
        previous: str | None = None
        for consumer in consumers:
            if consumer not in layers:
                findings.append(Finding(
                    "handoff-dag", iid, f"consumer {consumer!r} is not a declared layer"))
                continue
            if producer not in deps.get(consumer, set()):
                findings.append(Finding(
                    "handoff-dag", iid,
                    f"consumer {consumer} does not depend on producer {producer}",
                ))
            if previous is not None and previous not in deps.get(consumer, set()):
                findings.append(Finding(
                    "handoff-dag", iid,
                    f"declared mutation order breaks the DAG: {consumer} does not "
                    f"depend on prior consumer {previous}",
                ))
            previous = consumer
        obligations.append(Obligation(
            "identity_preservation", iid,
            "every entity crossing this handoff must keep one identity from producer "
            "through the final consumer; enforced by build-time evidence, registered here",
        ))

    def interface_covers(owner_layer: str, mutator: str) -> bool:
        for interface in interfaces:
            if str(interface.get("producer")) != owner_layer:
                continue
            if mutator in {str(c) for c in (interface.get("consumers") or [])}:
                return True
        return False

    for row in record.get("requirements") or []:
        rid = str(row.get("id") or "<requirement>")
        producer = str(row.get("producer") or "")
        if producer and producer not in layers:
            findings.append(Finding(
                "moment-observability", rid,
                f"producer {producer!r} is not a declared layer"))
            continue

        # 1. The assigned verification boundary must observe every declared moment.
        verify_at = row.get("verify_at") or {}
        kind = str(verify_at.get("kind") or "")
        moments = [int(m) for m in (row.get("moments") or [])]
        if kind not in VERIFY_KINDS:
            findings.append(Finding(
                "moment-observability", rid,
                f"verify_at.kind {kind!r} is not one of {sorted(VERIFY_KINDS)}",
            ))
        elif kind == "layer":
            verifier = str(verify_at.get("layer") or "")
            judged = {int(f) for f in (layers.get(verifier, {}).get("judge_frames") or [])}
            if verifier not in layers:
                findings.append(Finding(
                    "moment-observability", rid,
                    f"verify_at names unknown layer {verifier!r}"))
            else:
                unobservable = sorted(set(moments) - judged)
                if unobservable:
                    findings.append(Finding(
                        "moment-observability", rid,
                        f"declared moments {unobservable} are not judged at layer "
                        f"{verifier} (judged: {sorted(judged)}); verify cumulatively or "
                        "route to a boundary that observes them",
                    ))
        # acceptance evaluates the cumulative scene at every registered moment, and
        # harness verification replays deterministically: both observe any moment.

        # 2. At least one repair route must be able to mutate the implicated roles.
        implicated = [str(role) for role in (row.get("implicated_roles") or [])]
        routes = [str(route) for route in (row.get("repair_routes") or [])]
        if implicated:
            unknown_routes = [route for route in routes if route not in layers]
            for route in unknown_routes:
                findings.append(Finding(
                    "repair-reachability", rid,
                    f"repair route {route!r} is not a declared layer"))
            reachable = any(
                _patterns_overlap(role, pattern)
                for route in routes
                if route in layers
                for pattern in reserved.get(route, [])
                for role in implicated
            )
            if not reachable:
                findings.append(Finding(
                    "repair-reachability", rid,
                    f"no repair route in {routes or '[]'} reserves any implicated role "
                    f"{implicated}; a failure here would have no owner able to change "
                    "the responsible control",
                ))

        # 3. Downstream mutation of upstream state needs a declared handoff.
        if producer:
            for role in implicated:
                for owner_layer, patterns in reserved.items():
                    if owner_layer == producer:
                        continue
                    if not any(_patterns_overlap(role, pattern) for pattern in patterns):
                        continue
                    if not interface_covers(owner_layer, producer):
                        findings.append(Finding(
                            "handoff-declaration", rid,
                            f"producer {producer} mutates {role!r} reserved by layer "
                            f"{owner_layer} with no declared interface naming "
                            f"{producer} as a consumer of {owner_layer}",
                        ))

    return findings, obligations
