"""Annotate a replay journal with the artifact-policy violations it already contains.

The journal is the finalizer's designated input and the harness writes it verbatim from
accepted ``run_bpy`` calls. Its header teaches exactly one transformation — prune probes,
do not paste — and says nothing about the artifact execution policy, which rejects
constructs that are perfectly legal live. `obj = bpy.data.objects[name]` is an ordinary
live call and an invalid artifact, so a finalizer following the header exactly produces a
source the policy refuses. Two shots hit it, and on one the rejected line numbers matched
the journal's own content exactly (HIR-0216).

The harness owns both sides: it writes the journal and it owns the validator. So it states
which lines will be refused, with the legal form the policy already emits, instead of
letting the finalizer discover them by rejection. It does not rewrite the code — a
transformation that changes meaning is worse than a rejection.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vfx_harness.blender.artifact_execution import artifact_violations

SEPARATOR = "\n\n# ---- next accepted run_bpy call ----\n"
POLICY_HEADER = (
    "# ARTIFACT POLICY. These calls are journal entries, legal live. The unit script you\n"
    "# compose from them is validated by the artifact execution policy, which is stricter:\n"
    "# a bpy attribute chain, or a name bound to one, may only be read through further\n"
    "# attributes, invoked directly, or rebound to a simple name — never subscripted,\n"
    "# passed as an argument, stored, returned, or compared. Entries below carry a\n"
    "# POLICY note on every line that will be refused, with the legal form. Rewriting\n"
    "# those lines is part of composing the artifact, not an optional tidy-up.\n"
)


def annotate_journal(path: str | Path) -> int:
    """Insert a policy note above each journal entry that will be refused. Returns count."""

    journal = Path(path)
    try:
        text = journal.read_text(encoding="utf-8")
    except OSError:
        return 0
    head, _, rest = text.partition("\n\n")
    if not rest:
        return 0

    annotated: list[str] = []
    total = 0
    for entry in rest.split(SEPARATOR):
        try:
            tree = ast.parse(entry, filename="<journal-entry>", mode="exec")
        except SyntaxError:
            # A probe payload need not be a module; the finalizer prunes those anyway.
            annotated.append(entry)
            continue
        violations = artifact_violations(entry, tree)
        if not violations:
            annotated.append(entry)
            continue
        total += len(violations)
        note = "\n".join(
            f"# POLICY line {line}: {message}" for line, message in violations
        )
        annotated.append(f"{note}\n{entry}")

    if not total:
        return 0
    journal.write_text(
        head + "\n" + POLICY_HEADER + "\n" + SEPARATOR.join(annotated),
        encoding="utf-8",
    )
    return total


__all__ = ["POLICY_HEADER", "annotate_journal"]
