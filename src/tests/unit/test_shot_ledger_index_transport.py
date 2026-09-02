"""The reserved ``accepted_build`` ledger member changes only through the opaque index."""

from __future__ import annotations

import copy
import json
import pickle
from pathlib import Path

import pytest

from tests.unit.test_shot_ledger_v2 import _ledger
from vfx_harness.domain.brief import load_shot
from vfx_harness.orchestration.ledger import Ledger
from vfx_harness.orchestration.shot_ledger_index import (
    DerivedShotLedgerIndex,
    mint_derived_shot_ledger_index,
)
from vfx_harness.orchestration.shot_ledger_lock import LedgerSaveConflict
from vfx_harness.orchestration.shot_ledger_v2_derivation import (
    read_stored_shot_ledger_index,
)


def _shot(root: Path):
    (root / "brief.md").write_text(
        "---\nid: fixture\nframes: 1\nfps: 24\n---\nfixture brief\n",
        encoding="utf-8",
    )
    return load_shot(root)


def test_derived_index_is_opaque_and_uncopyable() -> None:
    ledger = _ledger()
    with pytest.raises(TypeError, match="minted only by the shot-ledger derivation writer"):
        DerivedShotLedgerIndex(ledger, key=object())
    derived = mint_derived_shot_ledger_index(ledger)
    assert derived.ledger == ledger
    assert derived.as_dict() == ledger.as_dict()
    with pytest.raises(TypeError):
        copy.copy(derived)
    with pytest.raises(TypeError):
        copy.deepcopy(derived)
    with pytest.raises(TypeError):
        pickle.dumps(derived)


def test_transport_publishes_only_the_opaque_index_and_preserves_it(tmp_path: Path) -> None:
    shot = _shot(tmp_path)
    ledger_value = _ledger()
    Ledger(shot).save(derived_index=mint_derived_shot_ledger_index(ledger_value))
    assert read_stored_shot_ledger_index(tmp_path) == ledger_value

    # An ordinary publication keeps the stored member untouched.
    plain = Ledger(shot)
    plain.data["seed"] = {"kept": True}
    plain.save()
    document = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert document["accepted_build"]["index_digest"] == ledger_value.index_digest
    assert document["seed"] == {"kept": True}

    # Hand-authored rows never reach the canonical ledger.
    forged = Ledger(shot)
    forged.data["accepted_build"] = {**ledger_value.as_dict(), "accepted_layers": []}
    with pytest.raises(LedgerSaveConflict, match="only through the derivation writer"):
        forged.save()
    assert read_stored_shot_ledger_index(tmp_path) == ledger_value
    assert list(tmp_path.glob(".shot.json.prepared.*")) == []

    # A raw dict is not an index, even with the right shape.
    with pytest.raises(LedgerSaveConflict, match="opaque index"):
        Ledger(shot).prepare_save(derived_index=ledger_value.as_dict())  # type: ignore[arg-type]
