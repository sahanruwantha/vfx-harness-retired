"""One row per model session, labelled by ROLE — so a cost can be attributed.

`run_layerN.json` records a layer total. That total cannot answer the question it most
needs to: layer 5 cost **$18.63 across 11 turns and 9,082 output tokens**, which is more
than layer 2's $9.77 across 37 turns. Aggregates said the expensive layer was the one that
barely ran, and nothing in the file could say why.

The answer was not in the builder at all. Layer 5 went 16 rounds and **every one of them
went to a best-of-three critic panel**, each juror carrying up to four 1568px images. The
money was in adjudication, and the builder's token counts were never going to show it.
Worse, `run_layerN.json` holds only the LAST run — layer 5's 16 rounds span several, so
the headline "$47.48 for five layers" is final attempts only; layer 2's discarded attempts
cost a further $78.76 by the improvement plan's own accounting.

So: a row per session, with the role attached, appended to `logs/cost.jsonl`. Then "what
did this layer spend on the critic" is a groupby instead of an inference.

    bind(shot_folder, role="critic", layer="5", round=3)
    ... run the session ...
    unbind()

Deliberately NOT per-request: the SDK reports usage at session end, and every role here
already runs its own session, so per-session-with-role carries the same information without
inventing a finer granularity than the data supports.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .log import log

NAME = "cost.jsonl"
_ctx: dict[str, Any] = {}


def bind(shot_folder: str | Path, role: str, **meta: Any) -> None:
    """Label every session recorded from here until unbind()."""
    _ctx.clear()
    _ctx.update({"folder": Path(shot_folder), "role": role, **meta})


def unbind() -> None:
    _ctx.clear()


def is_bound() -> bool:
    return bool(_ctx)


@contextmanager
def scoped(*, role: str, phase: str, **meta: Any):
    """Temporarily change cost attribution without leaking it into the next phase.

    Critic calls are nested inside a long-lived builder client.  A plain ``bind`` for the
    critic used to remain active until process exit, so the later finalizer was charged to
    the critic.  Restoring the complete prior context in ``finally`` makes every phase
    boundary explicit even when an SDK call raises.
    """
    previous = dict(_ctx)
    if not previous.get("folder"):
        raise RuntimeError("costlog.scoped requires an existing bound run")
    _ctx.update({"role": role, "phase": phase, **meta})
    try:
        yield
    finally:
        _ctx.clear()
        _ctx.update(previous)


def record(m: Any) -> None:
    """Append one row from a ResultMessage. Never raises: accounting that can break a
    $9 planning run is worse than accounting that is occasionally missing a row — but it
    says so out loud, because a silently empty ledger reads as 'nothing was spent'."""
    if not _ctx:
        return
    # ONLY the end-of-session result carries usage. The first version of this was hooked
    # into log_message (which the critic loop never calls, so critic cost went unrecorded);
    # the second called record() on every message in that loop and wrote 287 rows of zeros
    # for 6 sessions. Guard here rather than at each call site, so a future caller cannot
    # reintroduce either mistake.
    if type(m).__name__ != "ResultMessage" and getattr(m, "total_cost_usd", None) is None:
        return
    try:
        u = getattr(m, "usage", None) or {}
        get = u.get if isinstance(u, dict) else (lambda k, d=0: getattr(u, k, d))
        row = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "role": _ctx.get("role"),
            **{k: v for k, v in _ctx.items() if k not in ("folder", "role")},
            "model": getattr(m, "model", None),
            "subtype": getattr(m, "subtype", None),
            "turns": getattr(m, "num_turns", None),
            "duration_ms": getattr(m, "duration_ms", None),
            "cost_usd": getattr(m, "total_cost_usd", None),
            "input": get("input_tokens", 0),
            "output": get("output_tokens", 0),
            "cache_read": get("cache_read_input_tokens", 0),
            "cache_create": get("cache_creation_input_tokens", 0),
            "session_id": getattr(m, "session_id", None),
        }
        out = Path(_ctx["folder"]) / "logs" / NAME
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:
        log(f"! cost row not recorded: {str(e)[:120]}", 1)


def attempt_totals(shot_folder: str | Path, *, run_id: str, attempt: int) -> dict:
    """Aggregate every model session belonging to one concrete layer attempt."""
    path = Path(shot_folder) / "logs" / NAME
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("run_id") == run_id and row.get("attempt") == attempt:
                rows.append(row)
    # A streaming SDK client can emit several ResultMessages for one session and reports
    # cumulative usage each time.  Sum sessions, but take the maximum snapshot within a
    # session; otherwise a two-round builder is charged for round 1 twice.
    sessions: dict[str, list[dict]] = {}
    for index, row in enumerate(rows):
        key = str(row.get("session_id") or f"legacy-row-{index}")
        sessions.setdefault(key, []).append(row)
    collapsed = []
    for group in sessions.values():
        latest = group[-1]
        collapsed.append({
            **latest,
            "cost_usd": max(float(row.get("cost_usd") or 0.0) for row in group),
            "turns": max(int(row.get("turns") or 0) for row in group),
            **{key: max(int(row.get(key) or 0) for row in group)
               for key in ("input", "output", "cache_read", "cache_create")},
        })
    token_keys = ("input", "output", "cache_read", "cache_create")
    return {
        "cost_usd": sum(float(row.get("cost_usd") or 0.0) for row in collapsed),
        "turns": sum(int(row.get("turns") or 0) for row in collapsed),
        "tokens": {key: sum(int(row.get(key) or 0) for row in collapsed) for key in token_keys},
        "sessions": len(collapsed),
        "by_role": {
            role: round(sum(float(row.get("cost_usd") or 0.0) for row in collapsed
                            if row.get("role") == role), 6)
            for role in sorted({str(row.get("role")) for row in collapsed})
        },
    }


def summarise(shot_folder: str | Path) -> str:
    """Cost by role, and by layer — the two cuts the aggregates could not give."""
    path = Path(shot_folder) / "logs" / NAME
    if not path.is_file():
        return (f"no {NAME} yet — cost attribution starts with the next run "
                f"(the layer totals in run_layerN.json cannot be split by role)")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not rows:
        return f"{NAME} is empty"

    def _agg(key: str) -> list[str]:
        buckets: dict[str, dict[str, float]] = {}
        for r in rows:
            b = buckets.setdefault(str(r.get(key)), {"n": 0, "cost": 0.0, "out": 0})
            b["n"] += 1
            b["cost"] += r.get("cost_usd") or 0.0
            b["out"] += r.get("output") or 0
        total = sum(b["cost"] for b in buckets.values()) or 1.0
        return [f"     {k:<14} {int(b['n']):>3} session(s)  ${b['cost']:>7.2f} "
                f"({100 * b['cost'] / total:>4.1f}%)  {int(b['out']):>8,} out"
                for k, b in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"])]

    spend = sum(r.get("cost_usd") or 0.0 for r in rows)
    out = [f"── cost · {Path(shot_folder).name} · {len(rows)} session(s) · ${spend:.2f} ──",
           "   by role:", *_agg("role")]
    if any("layer" in r for r in rows):
        out += ["   by layer:", *_agg("layer")]
    return "\n".join(out)
