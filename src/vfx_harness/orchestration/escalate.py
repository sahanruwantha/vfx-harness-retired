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

    vfx escalate shots/<shot-id>            # show open questions
    vfx escalate shots/<shot-id> --answer 3 "<decision>"
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.observability import prepared_publication
from vfx_harness.observability.log import log

QUESTIONS = "questions.jsonl"
ANSWERS = "answers.md"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class PreparedQuestion:
    """One append-only question row staged as a complete CAS replacement."""

    update: prepared_publication.PreparedFileUpdate[tuple[int, bool]]
    question: str
    assumption: str


def _append_record(raw: bytes | None, record: dict) -> bytes:
    prefix = raw or b""
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    return prefix + json.dumps(record).encode("utf-8") + b"\n"


def _load_bytes(raw: bytes | None, path: Path) -> list[dict]:
    if raw is None:
        return []
    out = []
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # Preserve the existing operator-visible behavior.  The prepared
                # replacement keeps every original byte even when one legacy row is
                # unreadable; this parser never silently rewrites the event stream.
                log(
                    f"! {path.name}:{n} is not valid JSON and was SKIPPED ({exc}); "
                    "a question or answer may be missing"
                )
    merged: dict[int, dict] = {}
    for question in out:
        merged[question["id"]] = {
            **merged.get(question["id"], {}),
            **question,
        }
    return [merged[key] for key in sorted(merged)]


def prepare_question(
    shot_folder: str | Path,
    *,
    layer: str,
    question: str,
    assumption: str,
    why_it_matters: str = "",
    affected_layers: list[str] | None = None,
    affected_axes: list[str] | None = None,
    global_decision: bool = False,
    authority_binding: str,
) -> PreparedQuestion:
    """Prepare one logical append without holding selected/unit-state locks."""

    folder = Path(shot_folder)
    path = folder / QUESTIONS
    affected_layers = sorted({str(value) for value in (affected_layers or []) if str(value)})
    affected_axes = sorted({str(value) for value in (affected_axes or []) if str(value)})
    if not global_decision and not affected_layers and not affected_axes:
        raise ValueError(
            "question must declare affected_layers, affected_axes, or global_decision=true"
        )

    def build(raw: bytes | None) -> tuple[bytes | None, tuple[int, bool]]:
        existing = _load_bytes(raw, path)
        normalized = question.strip().lower()
        for row in existing:
            if row["question"].strip().lower() == normalized:
                return None, (int(row["id"]), False)
        qid = (max((row["id"] for row in existing), default=0)) + 1
        record = {
            "id": qid,
            "layer": layer,
            "question": question.strip(),
            "assumption": assumption.strip(),
            "why": why_it_matters.strip(),
            "affected_layers": affected_layers,
            "affected_axes": affected_axes,
            "global_decision": bool(global_decision),
            "asked": _now(),
            "answer": None,
        }
        return _append_record(raw, record), (qid, True)

    update = prepared_publication.prepare_file_update(
        folder,
        path,
        build,
        authority_binding=authority_binding,
    )
    return PreparedQuestion(
        update=update,
        question=question.strip(),
        assumption=assumption.strip(),
    )


def commit_prepared_question(
    prepared: PreparedQuestion,
    *,
    authority_binding: str,
) -> int:
    """Publish one prepared question and emit logs only after its CAS succeeds."""

    if prepared.update.publication is not None:
        prepared_publication.commit_prepared_file(
            prepared.update.publication,
            authority_binding=authority_binding,
        )
    return log_committed_question(prepared)


def log_committed_question(prepared: PreparedQuestion) -> int:
    """Emit operator diagnostics after the prepared metadata commit is released."""

    qid, created = prepared.update.result
    if created:
        log(f"❓ Q{qid} for the supervisor: {prepared.question[:100]}", 1)
        log(f"   proceeding on: {prepared.assumption[:100]}", 1)
    return qid


def discard_prepared_question(prepared: PreparedQuestion) -> None:
    prepared_publication.discard_prepared_file(prepared.update.publication)


def ask(shot_folder: str | Path, *, layer: str, question: str, assumption: str,
        why_it_matters: str = "", affected_layers: list[str] | None = None,
        affected_axes: list[str] | None = None, global_decision: bool = False) -> int:
    """Record a question through one prepared append-only CAS transaction."""

    prepared = prepare_question(
        shot_folder,
        layer=layer,
        question=question,
        assumption=assumption,
        why_it_matters=why_it_matters,
        affected_layers=affected_layers,
        affected_axes=affected_axes,
        global_decision=global_decision,
        authority_binding="supervisor-question",
    )
    try:
        return commit_prepared_question(
            prepared,
            authority_binding="supervisor-question",
        )
    except BaseException:
        discard_prepared_question(prepared)
        raise


def load(shot_folder: str | Path) -> list[dict]:
    path = Path(shot_folder) / QUESTIONS
    if not path.is_file():
        return []
    return _load_bytes(path.read_bytes(), path)


def answer(shot_folder: str | Path, qid: int, text: str) -> bool:
    folder = Path(shot_folder)
    path = folder / QUESTIONS

    def build(raw: bytes | None) -> tuple[bytes | None, bool]:
        questions = {row["id"]: row for row in _load_bytes(raw, path)}
        if qid not in questions:
            return None, False
        record = {"id": qid, "answer": text.strip(), "answered": _now()}
        return _append_record(raw, record), True

    answered = prepared_publication.publish_file_update(
        folder,
        path,
        build,
        authority_binding="supervisor-answer",
    )
    if not answered:
        return False
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
