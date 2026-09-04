"""Cross-row scene-contract contradictions no single row can reveal.

Each rule pairs rows that are individually valid but jointly unsatisfiable, so the
materialization gates and the plan gate refuse them before any builder spend
(HIR-0030, HIR-0175, HIR-0178). Single-row validation lives in ``validate``.
"""

from __future__ import annotations

from vfx_harness.domain.semantic_roles import match_semantic
from vfx_harness.evidence.scene_checks.validate import _optional_hi, _selectors, schedule_derivative_floor


def schedule_smoothness_contradictions(rows: list[dict]) -> list[dict[str, str]]:
    """Pair a schedule with a same-role derivative cap whose hi is below the linear floor."""
    schedules = [row for row in rows if isinstance(row, dict) and row.get("kind") == "keyframe_schedule"]
    derivatives = [row for row in rows if isinstance(row, dict) and row.get("kind") == "curve_derivative_max"]
    out: list[dict[str, str]] = []
    for deriv in derivatives:
        hi = deriv.get("hi")
        if isinstance(hi, bool) or not isinstance(hi, (int, float)):
            continue
        roles = tuple(sorted(str(role) for role in _selectors(deriv, "roles")))
        if not roles:
            continue
        prop = str(deriv.get("property") or "location")
        for schedule in schedules:
            if tuple(sorted(str(role) for role in _selectors(schedule, "roles"))) != roles:
                continue
            worst = schedule_derivative_floor(schedule, prop)
            if worst is None or float(worst["floor"]) <= float(hi) + 1e-9:
                continue
            schedule_id = str(schedule.get("id") or "<missing>")
            deriv_id = str(deriv.get("id") or "<missing>")
            span = int(worst["frame_b"]) - int(worst["frame_a"])
            out.append({
                "schedule_id": schedule_id,
                "smoothness_id": deriv_id,
                "message": (
                    f"{deriv_id}: hi {hi} is below the linear floor {worst['floor']:.6g} of "
                    f"{schedule_id} samples ({prop} Δ={worst['delta']:.6g} over frames "
                    f"{worst['frame_a']}→{worst['frame_b']}, {span} frames). Raise hi, widen "
                    "the span, or reduce Δ — interpolation cannot invent a third option"
                ),
            })
    return out


def _derivative_window(row: dict) -> tuple[int, int] | None:
    frames = row.get("frames")
    if isinstance(frames, (list, tuple)) and len(frames) == 2:
        try:
            lo, hi = int(frames[0]), int(frames[1])
        except (TypeError, ValueError):
            return None
        return (lo, hi) if lo <= hi else None
    return None


def derivative_bound_contradictions(rows: list[dict]) -> list[dict[str, str]]:
    """Pair a per-frame derivative floor with a same-role cap it can never clear.

    ``curve_derivative_max`` reads max(|Δ|) per adjacent frame over a window. A ``min``
    or ``band`` row demanding at least ``lo`` inside a window that a ``max`` or ``band``
    row caps at ``hi < lo`` over a containing window is unsatisfiable by construction:
    the required peak inside the inner window is also the peak the outer cap measures
    (run 20260902T190446Z-88aeb3 spent a builder session proving exactly that; HIR-0175).
    A partial overlap is not a contradiction — the peak may lie outside the cap.
    """
    derivatives = [
        row for row in rows if isinstance(row, dict) and row.get("kind") == "curve_derivative_max"
    ]
    out: list[dict[str, str]] = []
    for floor_row in derivatives:
        if floor_row.get("op") not in {"min", "band"}:
            continue
        lo = floor_row.get("lo")
        if isinstance(lo, bool) or not isinstance(lo, (int, float)):
            continue
        inner = _derivative_window(floor_row)
        if inner is None:
            continue
        roles = tuple(sorted(str(role) for role in _selectors(floor_row, "roles")))
        prop = str(floor_row.get("property") or "location")
        for cap_row in derivatives:
            if cap_row is floor_row or cap_row.get("op") not in {"max", "band"}:
                continue
            hi = _optional_hi(cap_row)
            if hi is None or float(lo) <= hi + 1e-9:
                continue
            if tuple(sorted(str(role) for role in _selectors(cap_row, "roles"))) != roles:
                continue
            if str(cap_row.get("property") or "location") != prop:
                continue
            outer = _derivative_window(cap_row)
            if outer is None or not (outer[0] <= inner[0] and inner[1] <= outer[1]):
                continue
            floor_id = str(floor_row.get("id") or "<missing>")
            cap_id = str(cap_row.get("id") or "<missing>")
            out.append({
                "floor_id": floor_id,
                "cap_id": cap_id,
                "message": (
                    f"{floor_id}: lo {lo} over frames {inner[0]}..{inner[1]} can never satisfy "
                    f"{cap_id}: hi {hi} over frames {outer[0]}..{outer[1]} on the same roles "
                    f"{list(roles)} and property {prop!r} — the peak the floor demands inside "
                    "the inner window is the same peak the cap measures. Lower lo below hi, "
                    "raise hi, or move the floor window outside the cap window; no curve "
                    "satisfies both"
                ),
            })
    return out


def _count_upper_bound(row: dict) -> float | None:
    """The largest host count an object_count row accepts, or None when unbounded."""
    op = str(row.get("op") or "")
    try:
        if op == "eq":
            return float(row.get("value")) + float(row.get("tol") or 0.0)
        if op in ("max", "band"):
            return float(row.get("hi"))
    except (TypeError, ValueError):
        return None
    return None


def _count_lower_bound(row: dict) -> int:
    """The smallest host count an object_count row demands; 1 when it only names a role.

    A sibling ``object_count min 12`` over a descendant namespace demands twelve hosts,
    not one. Reading it as a single host is what let ``eq 1`` over the parent survive
    beside it (HIR-0195).
    """
    op = str(row.get("op") or "")
    try:
        if op == "eq":
            return max(1, int(float(row.get("value")) - float(row.get("tol") or 0.0)))
        if op in ("min", "band"):
            return max(1, int(float(row.get("lo"))))
    except (TypeError, ValueError):
        return 1
    return 1


def _minimum_distinct_hosts(demands: dict[str, int]) -> int:
    """Hosts carry one role token, so each role with no descendant needs its own hosts.

    ``demands`` maps a descendant role to the number of hosts sibling rows require under
    it; a role that has a descendant of its own is counted through that descendant.
    """
    roles = set(demands)
    return sum(
        demands[role]
        for role in roles
        if not any(other != role and match_semantic(other, [role]) for other in roles)
    )


def namespace_count_contradictions(rows: list[dict]) -> list[dict]:
    """object_count bounds that the descendants required by sibling rows already exceed.

    Run 20260903T024933Z-7f07e7 authored ``object_count eq 1`` over the literal role
    ``camera.targets`` beside keyframe and projection rows on
    ``camera.targets.window_target`` and ``camera.targets.door_target``. The one
    canonical matcher (HIR-0147) counts a literal namespace's dotted descendants, so the
    row read 3 in every scene that satisfied its siblings, and the builder spent its
    session retagging hosts to learn that. Same-layer rows share the count row's
    cumulative scene; a ``persistent`` count row is re-evaluated under every later
    layer's descendants as well.
    """
    findings: list[dict] = []
    typed = [row for row in rows if isinstance(row, dict) and row.get("id")]
    for row in typed:
        if row.get("kind") != "object_count":
            continue
        selectors = [str(role) for role in _selectors(row, "roles") if str(role)]
        if not selectors or _selectors(row, "control_roles"):
            continue
        upper = _count_upper_bound(row)
        if upper is None:
            continue
        persistent = str(row.get("lifecycle") or "") == "persistent"
        owner = str(row.get("owner_layer") or "")
        descendants: dict[str, list[str]] = {}
        demands: dict[str, int] = {}
        for other in typed:
            # A sibling object_count is not excluded: a row demanding `min 12` under a
            # descendant namespace is the most direct statement that this bound cannot
            # hold, and skipping it hid exactly that pair (HIR-0195).
            if other is row:
                continue
            if not persistent and str(other.get("owner_layer") or "") != owner:
                continue
            for role in _selectors(other, "roles"):
                token = str(role)
                if token in selectors or not match_semantic(token, selectors):
                    continue
                descendants.setdefault(token, []).append(str(other.get("id")))
                required = (
                    _count_lower_bound(other)
                    if other.get("kind") == "object_count"
                    else 1
                )
                demands[token] = max(demands.get(token, 1), required)
        if not descendants:
            continue
        minimum = _minimum_distinct_hosts(demands)
        if minimum <= upper:
            continue
        bound = (
            f"eq {row.get('value')}" + (f" ± {row.get('tol')}" if row.get("tol") else "")
            if row.get("op") == "eq"
            else f"{row.get('op')} hi {row.get('hi')}"
        )
        witnesses = "; ".join(
            f"{token} required by {sorted(set(ids))}" for token, ids in sorted(descendants.items())
        )
        findings.append(
            {
                "id": str(row.get("id")),
                "roles": selectors,
                "descendants": sorted(descendants),
                "minimum": minimum,
                "message": (
                    f"{row.get('id')}: object_count {bound} over roles {selectors} can never "
                    "hold — a literal selector matches its dotted descendants (HIR-0147), "
                    f"and sibling rows require hosts tagged {witnesses}, so the count is at "
                    f"least {minimum}. Count a leaf role no descendant shares (for example "
                    f"'{selectors[0]}.root') or raise the bound to include those hosts"
                ),
            }
        )
    return findings


def validate_row_set(rows: list[dict]) -> list[str]:
    """Cross-row contradictions no single row can reveal.

    An auto-socket control_render_response demands a socket literally named 'Value'
    on THE one node its selector matches; a node_socket_value pinning the same
    (graph, node_roles) selector demands that node expose the pinned socket. Both
    published together in run 20260825 (world-bloom-response wanted 'Value' on the
    Glare its sibling pinned to 'Threshold' — CompositorNodeGlare exposes neither
    a 'Value' input nor output), and the contradiction only surfaced two builds
    and four repairs later. Explicitness costs one field; require it up front.

    A ``keyframe_schedule`` whose consecutive samples already exceed a same-role
    ``curve_derivative_max`` ``hi`` is the same class: the builder cannot satisfy
    both (run 20260826T170413Z-ba2b4c).
    """
    findings: list[str] = []
    def _selector(row: dict) -> tuple[str, tuple[str, ...]]:
        return (str(row.get("graph") or ""), tuple(sorted(_selectors(row, "node_roles"))))
    pinned: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "node_socket_value":
            continue
        if row.get("socket") or row.get("socket_index") is not None:
            pinned.setdefault(_selector(row), []).append(str(row.get("id") or "<missing>"))
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "control_render_response":
            continue
        if row.get("socket") or row.get("socket_index") is not None:
            continue
        selector = _selector(row)
        if selector[1] and selector in pinned:
            findings.append(
                f"{row.get('id', '<missing>')}: auto-socket control_render_response shares "
                f"selector {list(selector[1])} (graph {selector[0]!r}) with socket-pinned "
                f"row(s) {pinned[selector]} — one node cannot be required to expose both a "
                "literal 'Value' socket and the pinned socket; declare 'socket' on this row"
            )
    findings.extend(row["message"] for row in schedule_smoothness_contradictions(rows))
    findings.extend(row["message"] for row in derivative_bound_contradictions(rows))
    findings.extend(row["message"] for row in namespace_count_contradictions(rows))
    return findings
