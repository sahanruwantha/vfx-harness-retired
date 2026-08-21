"""Asking the supervisor — the escalation a professional makes instead of guessing.

Layers run unattended, so "ask the user" cannot mean "block". It means: write the question
down, proceed on the best available assumption, and MARK the work as resting on it. Then
a human answers a batch later and the answers become durable facts that every future layer
reads.

Without this, a genuinely ambiguous layer has one move: keep tuning. barrel_roll layer G
plateaued at 2.83 twice and burned ~$15 doing it, when the actual blocker was a question
worth ten seconds of a human's time (the references are 2:1, the brief said 16:9 — which
wins?). Nothing in the loop could form that question, let alone ask it.

    questions.jsonl   append-only, one JSON object per question
    answers.md        human-written; answers are injected into every later layer context

    python -m vfx_harness.orchestration.escalate shots/barrel_roll            # show open questions
    python -m vfx_harness.orchestration.escalate shots/barrel_roll --answer 3 "2:1 — match the refs"
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.observability.log import log

QUESTIONS = "questions.jsonl"
ANSWERS = "answers.md"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ask(shot_folder: str | Path, *, layer: str, question: str, assumption: str,
        why_it_matters: str = "", affected_layers: list[str] | None = None,
        affected_axes: list[str] | None = None, global_decision: bool = False) -> int:
    """Record a question, keep working on `assumption`. Returns the question id."""
    folder = Path(shot_folder)
    path = folder / QUESTIONS
    existing = load(folder)
    # don't re-ask the same thing every round
    for q in existing:
        if q["question"].strip().lower() == question.strip().lower():
            return q["id"]
    affected_layers = sorted({str(value) for value in (affected_layers or []) if str(value)})
    affected_axes = sorted({str(value) for value in (affected_axes or []) if str(value)})
    if not global_decision and not affected_layers and not affected_axes:
        raise ValueError(
            "question must declare affected_layers, affected_axes, or global_decision=true"
        )
    qid = (max((q["id"] for q in existing), default=0)) + 1
    rec = {"id": qid, "layer": layer, "question": question.strip(),
           "assumption": assumption.strip(), "why": why_it_matters.strip(),
           "affected_layers": affected_layers, "affected_axes": affected_axes,
           "global_decision": bool(global_decision),
           "asked": _now(), "answer": None}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    log(f"❓ Q{qid} for the supervisor: {question.strip()[:100]}", 1)
    log(f"   proceeding on: {assumption.strip()[:100]}", 1)
    return qid


def load(shot_folder: str | Path) -> list[dict]:
    path = Path(shot_folder) / QUESTIONS
    if not path.is_file():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                # A dropped line here can be an ANSWER, and an answer that vanishes turns
                # a settled question back into an unanswered one that blocks the build —
                # or, worse, silently reverts to the assumption.
                log(f"! {path.name}:{n} is not valid JSON and was SKIPPED ({e}); "
                    f"a question or answer may be missing")
    # later records for the same id win (that is how an answer lands)
    merged: dict[int, dict] = {}
    for q in out:
        merged[q["id"]] = {**merged.get(q["id"], {}), **q}
    return [merged[k] for k in sorted(merged)]


def answer(shot_folder: str | Path, qid: int, text: str) -> bool:
    folder = Path(shot_folder)
    qs = {q["id"]: q for q in load(folder)}
    if qid not in qs:
        return False
    with (folder / QUESTIONS).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": qid, "answer": text.strip(),
                             "answered": _now()}) + "\n")
    _rewrite_answers(folder)
    return True


def _rewrite_answers(folder: Path) -> None:
    answered = [q for q in load(folder) if q.get("answer")]
    if not answered:
        return
    body = ["# Supervisor answers", "",
            "Decisions from the human. These are LAW — they outrank inference from the "
            "brief or the stills, and they do not need re-deriving.", ""]
    for q in answered:
        body.append(f"**Q{q['id']} ({q['layer']}): {q['question']}**")
        body.append(f"→ {q['answer']}")
        body.append("")
    (folder / ANSWERS).write_text("\n".join(body), encoding="utf-8")


def answers_block(shot_folder: str | Path) -> str:
    """Answered questions, for injection into a layer's context. Empty if none."""
    answered = [q for q in load(shot_folder) if q.get("answer")]
    if not answered:
        return ""
    lines = ["SUPERVISOR ANSWERS — these are decided; treat as law, do not re-derive:"]
    lines += [f"  - {q['question']}  →  {q['answer']}" for q in answered]
    return "\n".join(lines)


def open_block(shot_folder: str | Path) -> str:
    """Unanswered questions + the assumption in force, so a layer stays consistent with
    what earlier layers already assumed."""
    unanswered = [q for q in load(shot_folder) if not q.get("answer")]
    if not unanswered:
        return ""
    lines = ["OPEN QUESTIONS (no answer yet — these assumptions are IN FORCE; stay "
             "consistent with them rather than choosing differently):"]
    lines += [f"  - {q['question']}  →  assuming: {q['assumption']}" for q in unanswered]
    return "\n".join(lines)


def unanswered_for_layer(shot_folder: str | Path, layer) -> list[dict]:
    """Open decisions that can actually change this layer or the whole shot.

    The legacy ``layer`` field identifies who *asked* the question (usually ``PLAN``);
    it is intentionally not used for routing.  Strict records carry an impact set.
    """
    layer_id = str(getattr(layer, "id", layer))
    axes = {str(axis) for axis in (getattr(layer, "owns", ()) or ())}
    out = []
    for q in load(shot_folder):
        if q.get("answer"):
            continue
        if not any(key in q for key in ("affected_layers", "affected_axes", "global_decision")):
            raise ValueError(
                f"Q{q.get('id', '?')} uses the removed question schema; add affected_layers, "
                "affected_axes, or global_decision"
            )
        if (q.get("global_decision")
                or layer_id in {str(value) for value in (q.get("affected_layers") or [])}
                or axes.intersection(str(value) for value in (q.get("affected_axes") or []))):
            out.append(q)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Questions a layer raised for the supervisor.")
    ap.add_argument("folder")
    ap.add_argument("--answer", nargs=2, metavar=("ID", "TEXT"))
    ap.add_argument("--all", action="store_true", help="include already-answered")
    args = ap.parse_args()
    folder = Path(args.folder)
    if args.answer:
        ok = answer(folder, int(args.answer[0]), args.answer[1])
        print("answered" if ok else "no such question id")
        return
    qs = load(folder)
    if not args.all:
        qs = [q for q in qs if not q.get("answer")]
    if not qs:
        print("no open questions")
        return
    for q in qs:
        mark = "✔" if q.get("answer") else "?"
        print(f"{mark} Q{q['id']} [{q['layer']}] {q['question']}")
        if q.get("why"):
            print(f"    matters because: {q['why']}")
        print(f"    assuming: {q['assumption']}")
        if q.get("answer"):
            print(f"    ANSWER: {q['answer']}")
        print()


if __name__ == "__main__":
    main()
