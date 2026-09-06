"""An amendment is bounded by the finding that drove it (HIR-0232, HIR-0245).

Split out of ``validate`` when that module crossed its line budget: this is one cohesive
question -- what did this amendment take away that nothing asked it to -- with its own
before-image resolution, and it does not belong wedged inside the general validator.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from vfx_harness.domain.amendment_scope import (
    AMENDMENT_RELAXATION_RULE,
    tightened_conflict_rows,
)
from vfx_harness.evidence.scene_checks import deferred_subject
from vfx_harness.orchestration.hypothesis_falsification_projection import (
    open_conflict_contract_ids,
)


def _rows_in_force(shot_folder, fallback: list[dict]) -> tuple[list[dict], str | None]:
    """The scene rows currently in force, for asking what an amendment took away.

    Returns the rows and, when they could not be read, why. A first materialization has
    no selected view and nothing in force, so nothing of its can shrink and the caller's
    base is the right fallback -- but a selected view that exists and cannot be read is a
    different thing, and this check going quiet is how HIR-0232 came to be inert in the
    first place. The caller notes it rather than swallowing it (HIR-0245).
    """
    try:
        rows = deferred_subject.load_rows(shot_folder)
    except FileNotFoundError:
        return fallback, None
    except Exception as exc:  # reported to the caller, never silently dropped
        return fallback, f"{type(exc).__name__}: {exc}"
    return (rows or fallback), None


def amendment_scope_findings(
    shot_folder: Any,
    base_scene_rows: Sequence[dict],
    scene_rows: Sequence[dict],
) -> Iterator[str]:
    """Messages for every row a finding-driven amendment narrowed.

    The before-image is the SELECTED view, not the design base. On a rematerialization the
    design base is the reverted overlay HIR-0026 writes, which strips every row the target
    layer owns -- so on the controller-dispatched amendment this check exists to police,
    the base was guaranteed to contain none of the named rows and the comparison never ran.
    One parameter answering two questions: what the materializer designs from, and what was
    admissible before (HIR-0245).
    """
    in_force_rows, in_force_error = _rows_in_force(shot_folder, list(base_scene_rows))
    if in_force_error:
        yield (
            "amendment-scope check fell back to the design base: the selected view's "
            f"scene contracts could not be read ({in_force_error}). A row this amendment "
            "narrowed may not be reported."
        )
    for record_id, conflict_ids in open_conflict_contract_ids(shot_folder):
        for tightened in tightened_conflict_rows(conflict_ids, in_force_rows, scene_rows):
            yield (
                f"amendment for finding {record_id} {tightened.describe()}, which its "
                f"conflict did not put in question. {AMENDMENT_RELAXATION_RULE}"
            )
