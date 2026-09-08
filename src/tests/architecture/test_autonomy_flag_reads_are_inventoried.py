"""Every raw read of the autonomy flag is counted, so a fifth one cannot appear quietly.

HIR-0205 converted three consumers that asked the autonomy question about a bound id.
A fourth survived in ``LayerReplayPointObservation.mint`` and killed a shot two days
later. The search that found the three was truncated and its visible subset was treated
as complete (HIR-0210).

This test does not forbid reading ``authoritative`` -- an unbound row genuinely needs it.
It pins the exact inventory, so adding a read is a deliberate edit to this list with a
reason, and removing one is visible progress.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "vfx_harness"
_READ = re.compile(r'\.get\("authoritative"\)|\["authoritative"\]')

# Modules that still decide something by reading the flag directly, with the count of
# reads in each. `domain/evidence_authority.py` is the one legitimate owner and is
# excluded. Shrinking this map is the direction of travel; growing it needs a reason.
EXPECTED = {
    "agents/acceptance.py": 3,
    "agents/acceptance_stop.py": 2,
    "agents/acceptance_stop_evidence.py": 2,
    "agents/builder/axes.py": 1,
    "agents/builder/critic_focus.py": 3,
    "agents/builder/revalidate.py": 1,
    "agents/builder/verify.py": 1,
    "blender/tools/mutate.py": 3,
    "blender/tools/reports.py": 2,
    "domain/layer_outcome_projections.py": 2,
    "domain/layer_outcomes.py": 1,
    "domain/layer_replay_observation_values.py": 1,
    "orchestration/revalidation.py": 1,
}


def _inventory() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE).as_posix()
        if rel == "domain/evidence_authority.py":
            continue
        count = len(_READ.findall(path.read_text(encoding="utf-8")))
        if count:
            found[rel] = count
    return found


def test_the_raw_autonomy_reads_are_exactly_the_recorded_inventory() -> None:
    found = _inventory()

    added = {k: v for k, v in found.items() if k not in EXPECTED}
    assert not added, (
        "a new module reads the autonomy flag directly. If it is deciding about a BOUND "
        "id, call domain.evidence_authority instead — that confusion has cost three "
        f"shots. If it genuinely concerns an unbound row, add it here with a reason: {added}"
    )
    removed = sorted(set(EXPECTED) - set(found))
    changed = {k: (EXPECTED[k], found[k]) for k in found if k in EXPECTED and found[k] != EXPECTED[k]}
    assert not removed and not changed, (
        "the inventory moved; update EXPECTED in the same change. "
        f"gone: {removed}; count changed (expected, found): {changed}"
    )


def test_the_receipts_that_seal_evidence_no_longer_read_it_raw() -> None:
    """The record-of-record path decides through the shared predicates only."""
    for rel in (
        "domain/layer_replay_observations.py",
        "domain/layer_evaluation_receipts.py",
        "domain/layer_finalization_receipts.py",
        "orchestration/unit_evaluation_receipts.py",
        "evidence/claim_evidence.py",
    ):
        body = (SOURCE / rel).read_text(encoding="utf-8")
        assert not _READ.search(body), f"{rel} reads the autonomy flag directly"
        assert "evidence_authority" in body, f"{rel} does not use the shared predicates"


# Modules that PRODUCE the sealed `authoritative` list and modules that RE-DERIVE it must
# select rows the same way, or the two sets differ and the projection refuses its own
# record. Converting one side alone is what HIR-0213 cost: the producers kept every typed
# measurement while the re-derivation still filtered on autonomy, so they differed by
# exactly the builder-paid image rows.
SEALED_EVIDENCE_SELECTORS = (
    "domain/layer_finalization_receipts.py",   # produces the point projection
    "orchestration/revalidation.py",           # produces the sealed canonical projection
    "domain/layer_outcome_projections.py",     # re-derives it from the terminal verdicts
    "agents/builder/verify.py",                # produces evidence_failures
    "domain/layer_evaluation_receipts.py",     # re-derives evidence_failures
)


def test_every_selector_of_sealed_evidence_uses_the_shared_predicate() -> None:
    for rel in SEALED_EVIDENCE_SELECTORS:
        body = (SOURCE / rel).read_text(encoding="utf-8")
        assert "evidence_authority.is_recorded_evidence" in body, (
            f"{rel} selects sealed evidence without the shared predicate; a producer and "
            "its re-derivation must agree or the projection cannot verify its own record"
        )
