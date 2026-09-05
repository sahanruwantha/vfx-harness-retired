"""The journal names the lines the artifact policy will refuse (HIR-0216).

Two shots produced journals in the rejected dialect, and on caesar the rejected line
numbers matched the journal's own content exactly:

    "line 21 uses `obj` (a handle on bpy.context.active_object)"   journal line 20
    "line 72 uses `bpy.data.objects` ..."                          journal line 72

The header taught one transformation — prune probes, do not paste — and said nothing
about the policy. A finalizer following it exactly produces an invalid artifact.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from vfx_harness.blender.artifact_execution import (
    ArtifactExecutionPolicyError,
    artifact_violations,
    validate_artifact_source,
)
from vfx_harness.blender.journal_policy import SEPARATOR, annotate_journal

LIVE_BUT_INVALID = 'import bpy\nname = "x"\nobj = bpy.data.objects[name]\n'


def test_every_violation_is_found_in_one_walk() -> None:
    """Three findings of one gate over one input, previously reported serially."""
    source = 'import bpy\nn = "x"\na = bpy.data.objects[n]\nb = bpy.data.objects[n]\n'

    found = artifact_violations(source, ast.parse(source))

    assert [line for line, _ in found] == [3, 4], "source order, not ast.walk order"
    assert all("bpy.data.objects" in message for _line, message in found)


def test_the_refusal_lists_all_of_them_and_says_so() -> None:
    source = 'import bpy\nn = "x"\na = bpy.data.objects[n]\nb = bpy.data.objects[n]\n'

    with pytest.raises(ArtifactExecutionPolicyError) as caught:
        validate_artifact_source(source)

    message = str(caught.value)
    assert "2 policy violations" in message
    assert "fix all of them in one edit" in message
    assert message.count("line 3") == 1 and message.count("line 4") == 1


def test_a_single_violation_keeps_its_exact_message() -> None:
    """One finding must not gain a list header; HIR-0174's text is unchanged."""
    with pytest.raises(ArtifactExecutionPolicyError) as caught:
        validate_artifact_source(LIVE_BUT_INVALID)

    message = str(caught.value)
    assert "policy violations" not in message
    assert message.startswith("artifact bpy capability cannot escape")


def test_legal_source_is_still_accepted() -> None:
    """Collecting must not admit anything: the legal forms stay legal."""
    for source in (
        'import bpy\nobj = bpy.data.objects.get("x")\n',
        "import bpy\nlast = bpy.context.active_object\n",
        'import bpy\nbpy.data.objects.new("x", None)\n',
    ):
        assert validate_artifact_source(source) is not None


def test_the_journal_annotates_the_lines_that_will_be_refused(tmp_path: Path) -> None:
    journal = tmp_path / "layer-2@unit.py"
    journal.write_text(
        "# JOURNAL — 2 active-unit run_bpy calls, in order.\n"
        "# Superseded tweaks and probes included: PRUNE, do not paste.\n\n"
        + LIVE_BUT_INVALID
        + SEPARATOR
        + 'import bpy\nbpy.data.objects.new("ok", None)\n',
        encoding="utf-8",
    )

    flagged = annotate_journal(journal)
    body = journal.read_text(encoding="utf-8")

    assert flagged == 1
    assert "ARTIFACT POLICY" in body
    assert "# POLICY line 3:" in body
    assert "cannot escape a tracked attribute" in body
    # The original content survives verbatim — nothing is rewritten.
    assert "obj = bpy.data.objects[name]" in body
    assert 'bpy.data.objects.new("ok", None)' in body
    # The clean entry gains no note.
    assert body.count("# POLICY line") == 1


def test_a_clean_journal_is_left_byte_identical(tmp_path: Path) -> None:
    journal = tmp_path / "clean.py"
    original = (
        "# JOURNAL — 1 active-unit run_bpy calls, in order.\n\n"
        'import bpy\nbpy.data.objects.new("x", None)\n'
    )
    journal.write_text(original, encoding="utf-8")

    assert annotate_journal(journal) == 0
    assert journal.read_text(encoding="utf-8") == original


def test_an_unparseable_entry_is_left_alone(tmp_path: Path) -> None:
    """A probe payload need not be a module, and the finalizer prunes those anyway."""
    journal = tmp_path / "probe.py"
    journal.write_text("# JOURNAL\n\nnot valid python (((\n", encoding="utf-8")

    assert annotate_journal(journal) == 0
