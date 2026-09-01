"""Public boundary for reviewed layer-finalization release."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vfx_harness.application import finalization_admin
from vfx_harness.cli import _COMMANDS
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceActive,
    BuilderExecutionFenceLease,
    require_builder_execution_lease,
)


def test_public_finalizations_release_passes_exact_reviewed_inputs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = SimpleNamespace(selection_token=SimpleNamespace())
    layer = SimpleNamespace(id="form", stages=(), script="build/form.py")
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        finalization_admin,
        "load_shot",
        lambda _folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        finalization_admin,
        "resolve_selected_authority",
        lambda _folder: selected,
    )
    monkeypatch.setattr(
        finalization_admin,
        "load_layers",
        lambda _shot, *, selected_authority: {"form": layer},
    )

    def release(
        shot_folder,
        selected_layer,
        *,
        claim_id,
        selected_authority,
        reason,
        evidence,
        fence_lease,
    ):
        assert isinstance(fence_lease, BuilderExecutionFenceLease)
        require_builder_execution_lease(fence_lease, tmp_path)
        observed.update(
            {
                "shot": shot_folder,
                "layer": selected_layer,
                "claim_id": claim_id,
                "selected": selected_authority,
                "reason": reason,
                "evidence": evidence,
            }
        )
        return SimpleNamespace(
            receipt=SimpleNamespace(
                as_dict=lambda: {
                    "schema": "vfx-harness.layer-finalization-release-receipt/v2"
                }
            ),
            locator="state/layer-finalization-releases/lfr-fixture.json",
            sha256="a" * 64,
        )

    monkeypatch.setattr(finalization_admin, "release_active_layer_finalization", release)

    assert (
        finalization_admin.main(
            [
                "release",
                str(tmp_path),
                "--layer",
                "form",
                "--claim-id",
                "lfc-exact",
                "--reason",
                "reviewed process death",
                "--evidence",
                "runs/dead/evidence/process.json",
                "--evidence",
                "runs/dead/evidence/critic.json",
            ]
        )
        == 0
    )

    assert observed == {
        "shot": tmp_path,
        "layer": layer,
        "claim_id": "lfc-exact",
        "selected": selected,
        "reason": "reviewed process death",
        "evidence": (
            "runs/dead/evidence/process.json",
            "runs/dead/evidence/critic.json",
        ),
    }
    output = json.loads(capsys.readouterr().out)
    assert output["locator"].startswith("state/layer-finalization-releases/")
    assert output["release_receipt"]["schema"].endswith("/v2")


def test_public_release_refuses_a_live_builder(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        finalization_admin,
        "load_shot",
        lambda _folder: SimpleNamespace(folder=tmp_path),
    )

    class ActiveFence:
        def __enter__(self):
            raise BuilderExecutionFenceActive("fixture live owner")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        finalization_admin,
        "builder_execution_fence",
        lambda _folder: ActiveFence(),
    )

    with pytest.raises(SystemExit, match="live builder still owns the shot"):
        finalization_admin.main(
            [
                "release",
                str(tmp_path),
                "--layer",
                "form",
                "--claim-id",
                "lfc-exact",
                "--reason",
                "reviewed process death",
                "--evidence",
                "runs/dead/evidence/process.json",
            ]
        )


def test_finalization_release_is_a_separate_top_level_command() -> None:
    assert (
        _COMMANDS["finalizations"]
        == "vfx_harness.application.finalization_admin:main"
    )
    assert "finalizations" != "units"
