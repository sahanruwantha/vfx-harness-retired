from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

publish = importlib.import_module(
    "vfx_harness.orchestration.jit_materialization.publish"
)


def _write_base_documents(root: Path) -> dict[str, Path]:
    documents = {
        "layers.json": {"schema": 5, "layers": []},
        "scene_checks.json": {"schema": 1, "contracts": []},
        "checks.json": {"schema": 1, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [],
    }
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        paths[name] = path
    return paths


def _final_documents(decision_strength: str) -> dict:
    return {
        "layers.json": {
            "schema": 5,
            "layers": [{"id": "look", "execution": "ready"}],
        },
        "scene_checks.json": {"schema": 1, "contracts": []},
        "checks.json": {"schema": 1, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [
                {
                    "id": "R1",
                    "statement": "The hall reads as the approved reference.",
                    "citation": {
                        "source": "brief.md",
                        "sha256": "b" * 64,
                        "line_start": 1,
                        "line_end": 1,
                    },
                    "resolution": {
                        "kind": "decision",
                        "ids": [],
                        "decision": "The hall reads as the approved reference.",
                        "decision_strength": decision_strength,
                        "evidence_domains": ["human"],
                        "domain_bindings": [
                            {
                                "domain": "human",
                                "kind": "provisional_decision",
                                "statement": "The hall reads as the approved reference.",
                                "decision_strength": decision_strength,
                            }
                        ],
                    },
                }
            ],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [],
    }


def test_publish_view_hash_covers_final_requirement_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    bases = _write_base_documents(tmp_path)
    bundle = SimpleNamespace(content_hash="a" * 64)
    materialized = SimpleNamespace(
        layer_row={"id": "look", "execution": "ready"},
        scene_contracts=(),
        image_contracts=(),
        acceptance=(),
    )
    final_documents = iter(
        (
            _final_documents("approved_start"),
            _final_documents("planner_start"),
        )
    )
    monkeypatch.setattr(
        publish,
        "_composed_documents",
        lambda *_args, **_kwargs: (bundle, materialized, bases),
    )
    monkeypatch.setattr(
        publish,
        "_overlay_documents",
        lambda *_args, **_kwargs: deepcopy(next(final_documents)),
    )

    candidate = tmp_path / "candidate.json"
    first_pointer_path = publish.publish_materialization(tmp_path, candidate)
    first_pointer = json.loads(first_pointer_path.read_text(encoding="utf-8"))
    first_requirements = tmp_path / first_pointer["artifacts"]["requirements.json"]

    second_pointer_path = publish.publish_materialization(tmp_path, candidate)
    second_pointer = json.loads(second_pointer_path.read_text(encoding="utf-8"))
    second_requirements = tmp_path / second_pointer["artifacts"]["requirements.json"]

    assert first_pointer["view_hash"] != second_pointer["view_hash"]
    assert first_requirements.parent != second_requirements.parent
    assert (
        json.loads(first_requirements.read_text(encoding="utf-8"))["requirements"][0]
        ["resolution"]["decision_strength"]
        == "approved_start"
    )
    assert (
        json.loads(second_requirements.read_text(encoding="utf-8"))["requirements"][0]
        ["resolution"]["decision_strength"]
        == "planner_start"
    )
