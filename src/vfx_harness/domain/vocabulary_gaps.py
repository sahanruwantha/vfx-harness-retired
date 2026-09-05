"""Where a shot records the vocabulary gaps that make a decision legal.

One source of truth for the path. ``escalate_vocabulary_gap`` wrote these rows under
the shot folder while the materialization validator read them under the *plan bundle*
root, so every recorded gap was invisible to the rule that asks for one. The reader's
``except OSError: return {}`` turned that disagreement into silence rather than a
failure, and a materializer recorded the same gap three times against a refusal that
could never change (HIR-0218).

The path is a function here so a caller cannot hold a different opinion about it, and
the reader takes the shot folder by name rather than a generic ``root``.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "vfx-harness.vocabulary-gap/v1"


def vocabulary_gaps_path(shot_folder: str | Path) -> Path:
    """The one durable location for a shot's recorded vocabulary gaps."""
    return Path(shot_folder) / "state" / "plan-escalations" / "vocabulary-gaps.jsonl"


def recorded_vocabulary_gap_ids(shot_folder: str | Path) -> dict[str, tuple[str, ...]]:
    """Vocabulary-gap ids this shot recorded, by requirement id.

    Rows this reader cannot interpret are skipped rather than raised: the file is an
    append-only journal, so a torn trailing line is a real possibility and a validator
    that dies on one would strand a shot in state an operator may not hand-edit. That
    tolerance was never the defect. Reading it from the wrong directory was — and the
    two were indistinguishable, because both produced an empty result.
    """
    path = vocabulary_gaps_path(shot_folder)
    if not path.is_file():
        return {}
    gaps: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            continue
        requirement_id = str(row.get("requirement_id") or "")
        gap_id = str(row.get("id") or "")
        if requirement_id and gap_id:
            gaps.setdefault(requirement_id, []).append(gap_id)
    return {key: tuple(value) for key, value in gaps.items()}
