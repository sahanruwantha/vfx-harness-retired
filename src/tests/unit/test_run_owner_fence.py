"""Run-owner inode fence and immutable-claim adapter contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

import vfx_harness.observability.run_owner_fence as run_owner_fence_module
import vfx_harness.observability.run_owner_fence_files as run_owner_fence_files_module
import vfx_harness.observability.run_owner_fork_guard as run_owner_fork_guard_module
from vfx_harness.domain.run_owner_claims import RunOwnerClaim
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.observability.run_owner_fence import (
    RUN_OWNER_CLAIM,
    RUN_OWNER_FENCE,
    RunOwnerClaimExists,
    RunOwnerFenceActive,
    RunOwnerFenceError,
    RunOwnerFenceSubstituted,
    acquire_run_owner_fence,
    acquire_run_reconciler_fence,
    read_run_owner_claim,
)
from vfx_harness.observability.run_owner_manifest import RUN_DISPATCH_SCHEMA, RUN_MANIFEST_SCHEMA


def _run_root(tmp_path: Path, run_id: str = "run-001") -> tuple[Path, dict[str, object]]:
    root = tmp_path / "shot" / "runs" / run_id
    root.mkdir(parents=True)
    manifest: dict[str, object] = {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": run_id,
        "shot_id": "fixture",
        "started_at": "2026-09-01T12:00:00+00:00",
        "invocation": {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": "direct",
                "command": "plan",
            },
            "argv": ["vfx plan", "fixture"],
            "parameters": {},
        },
        "layout": {
            "status": "status.json",
            "artifact_index": "artifacts.json",
            "logs": "logs/",
            "reports": "reports/",
            "evidence": "evidence/",
            "checkpoints": "checkpoints/",
            "scratch": "scratch/",
            "deliverables": "deliverables/",
            "owner_claim": "owner/claim.json",
            "owner_fence": "owner/fence.lock",
        },
        "authority": {
            "authored_inputs": "../../brief.md and ../../refs/",
            "published_plan": "../../plans/current.json when present",
            "plan_authoring_workspace": "scratch/plan-workspace/ for global plan invocations",
            "selected_plan_consumers": "../../plans/current.json",
            "accepted_build": "../../build/ and ../../shot.json",
            "generated_output": "this directory",
        },
        "reader_entrypoint": "manifest.json",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, manifest


def _child_environment() -> dict[str, str]:
    repository = Path(__file__).resolve().parents[3]
    source = str(repository / "src")
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not existing else f"{source}{os.pathsep}{existing}"
    return environment


def test_owner_mints_and_reads_back_claim_bound_to_manifest_and_fence(
    tmp_path: Path,
) -> None:
    root, manifest = _run_root(tmp_path)
    expected_manifest_sha = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    expected_invocation_digest = canonical_digest(manifest["invocation"])

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as lease:
        claim = lease.claim
        observed = read_run_owner_claim(
            root,
            run_id="run-001",
            expected_digest=claim.digest,
        )

        assert observed == claim
        assert claim.invocation_digest == expected_invocation_digest
        assert claim.manifest_sha256 == expected_manifest_sha
        assert claim.fence_locator == "owner/fence.lock"
        assert claim.fence_implementation == "posix-flock-inode/v1"
        assert claim.owner_kind == "direct"
        assert claim.process_id == os.getpid()
        assert (claim.shot_root_device, claim.shot_root_inode) == (
            root.parent.parent.stat().st_dev,
            root.parent.parent.stat().st_ino,
        )
        assert (claim.runs_directory_device, claim.runs_directory_inode) == (
            root.parent.stat().st_dev,
            root.parent.stat().st_ino,
        )
        assert (claim.run_root_device, claim.run_root_inode) == (
            root.stat().st_dev,
            root.stat().st_ino,
        )
        assert (claim.owner_directory_device, claim.owner_directory_inode) == (
            (root / "owner").stat().st_dev,
            (root / "owner").stat().st_ino,
        )
        assert (claim.claim_device, claim.claim_inode) == (
            (root / RUN_OWNER_CLAIM).stat().st_dev,
            (root / RUN_OWNER_CLAIM).stat().st_ino,
        )
        assert lease.descriptor_inheritable is False
        assert lease.fence_identity == (
            (root / RUN_OWNER_FENCE).stat().st_dev,
            (root / RUN_OWNER_FENCE).stat().st_ino,
        )
        assert RunOwnerClaim.from_dict(json.loads((root / RUN_OWNER_CLAIM).read_text(encoding="utf-8"))) == claim

    with acquire_run_reconciler_fence(root, prior_owner=claim) as reconciler:
        assert reconciler.acquisition_kind == "reconciler"
        assert reconciler.claim == claim
        assert reconciler.descriptor_inheritable is False


def test_owner_claim_cannot_predate_the_verified_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _run_root(tmp_path)
    started_at = manifest["started_at"]
    assert isinstance(started_at, str)
    monkeypatch.setattr(
        run_owner_fence_module,
        "_now",
        lambda: "2026-09-01T11:59:59+00:00",
    )

    with pytest.raises(RunOwnerFenceError, match="manifest start/owner claim"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert not (root / RUN_OWNER_CLAIM).exists()


def test_owner_claim_may_equal_the_verified_manifest_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _run_root(tmp_path)
    started_at = manifest["started_at"]
    assert isinstance(started_at, str)
    monkeypatch.setattr(run_owner_fence_module, "_now", lambda: started_at)

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as lease:
        assert lease.claim.claimed_at == started_at


@pytest.mark.parametrize(
    ("command", "owner_kind"),
    (("build", "direct"), ("plan", "driver")),
)
def test_owner_refuses_labels_that_disagree_with_verified_invocation(
    tmp_path: Path,
    command: str,
    owner_kind: str,
) -> None:
    root, _manifest = _run_root(tmp_path)

    with pytest.raises(RunOwnerFenceError, match="disagree with the verified manifest invocation"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command=command,
            owner_kind=owner_kind,
        )

    assert not (root / RUN_OWNER_CLAIM).exists()


def test_driver_identity_is_derived_from_closed_run_invocation_shape(
    tmp_path: Path,
) -> None:
    root, manifest = _run_root(tmp_path)
    manifest["invocation"] = {
        "dispatch": {
            "schema": RUN_DISPATCH_SCHEMA,
            "kind": "driver",
            "command": "run",
        },
        "argv": ["vfx run", "fixture"],
        "parameters": {"rounds": 2},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="run",
        owner_kind="driver",
    ) as lease:
        assert lease.claim.command == "run"
        assert lease.claim.owner_kind == "driver"


@pytest.mark.parametrize(
    ("command", "owner_kind"),
    (
        ("plan", "direct"),
        ("build", "direct"),
        ("accept", "direct"),
        ("render", "direct"),
        ("run", "driver"),
    ),
)
def test_owner_accepts_exact_public_cli_dispatch_shapes(
    tmp_path: Path,
    command: str,
    owner_kind: str,
) -> None:
    root, manifest = _run_root(tmp_path)
    manifest["invocation"] = {
        "dispatch": {
            "schema": RUN_DISPATCH_SCHEMA,
            "kind": owner_kind,
            "command": command,
        },
        "argv": [f"vfx {command}", "fixture"],
        "parameters": {"rounds": 2} if owner_kind == "driver" else {},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command=command,
        owner_kind=owner_kind,
    ) as lease:
        assert lease.claim.command == command
        assert lease.claim.owner_kind == owner_kind


@pytest.mark.parametrize(
    "invocation",
    (
        {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": "direct",
                "command": "plan",
            },
            "argv": ["vfx build", "fixture"],
            "parameters": {},
        },
        {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": "driver",
                "command": "plan",
            },
            "argv": ["vfx plan", "fixture"],
            "parameters": {},
        },
        {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": "direct",
                "command": "run",
            },
            "argv": ["vfx run", "fixture"],
            "parameters": {},
        },
        {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": "direct",
                "command": "asset",
            },
            "argv": ["vfx asset", "fixture"],
            "parameters": {},
        },
        {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": "direct",
                "command": "plan",
            },
            "argv": ["vfx plan", "fixture"],
            "parameters": {"command": "plan", "direct": True},
        },
    ),
)
def test_owner_refuses_ambiguous_or_inconsistent_invocation_shape(
    tmp_path: Path,
    invocation: dict[str, object],
) -> None:
    root, manifest = _run_root(tmp_path)
    manifest["invocation"] = invocation
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RunOwnerFenceError, match=r"dispatch|invocation"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert not (root / RUN_OWNER_CLAIM).exists()


@pytest.mark.parametrize("mutation", ("legacy-schema", "top-level-extra", "layout-extra"))
def test_owner_refuses_non_v2_or_open_manifest_shapes(
    tmp_path: Path,
    mutation: str,
) -> None:
    root, manifest = _run_root(tmp_path)
    if mutation == "legacy-schema":
        manifest["schema"] = "vfx-harness.run/v1"
    elif mutation == "top-level-extra":
        manifest["unexpected"] = True
    else:
        layout = manifest["layout"]
        assert isinstance(layout, dict)
        layout["unexpected"] = "scratch/escape/"
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RunOwnerFenceError, match=r"schema|fields mismatch"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert not (root / RUN_OWNER_CLAIM).exists()


@pytest.mark.parametrize("encoding", ("utf-16", "utf-32"))
def test_owner_refuses_a_non_utf8_manifest(
    tmp_path: Path,
    encoding: str,
) -> None:
    root, manifest = _run_root(tmp_path)
    (root / "manifest.json").write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(encoding))

    with pytest.raises(RunOwnerFenceError, match="strict JSON object"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert not (root / RUN_OWNER_CLAIM).exists()


def test_owner_reuses_preexisting_regular_fence_inode(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    fence = root / RUN_OWNER_FENCE
    fence.parent.mkdir()
    fence.touch(mode=0o600)
    expected = (fence.stat().st_dev, fence.stat().st_ino)

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as lease:
        assert lease.fence_identity == expected
        assert (lease.claim.fence_device, lease.claim.fence_inode) == expected

    assert (fence.stat().st_dev, fence.stat().st_ino) == expected


def test_reconciler_refuses_a_live_owner_without_pid_or_time_inference(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim_bytes = (root / RUN_OWNER_CLAIM).read_bytes()
        with pytest.raises(RunOwnerFenceActive, match="live root invocation"):
            acquire_run_reconciler_fence(root, prior_owner=owner.claim)
        assert (root / RUN_OWNER_CLAIM).read_bytes() == claim_bytes


def test_reconciler_refuses_a_substituted_fence_inode(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim = owner.claim

    fence = root / RUN_OWNER_FENCE
    retired = fence.with_name("fence.retired")
    fence.rename(retired)
    fence.touch(mode=0o600)

    with pytest.raises(RunOwnerFenceSubstituted, match="inode bound by its claim"):
        acquire_run_reconciler_fence(root, prior_owner=claim)
    with pytest.raises(RunOwnerFenceSubstituted, match="inode bound by its claim"):
        read_run_owner_claim(root, run_id="run-001")


def test_reader_refuses_exact_claim_bytes_copied_to_a_new_inode(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim = owner.claim

    claim_path = root / RUN_OWNER_CLAIM
    retired = claim_path.with_name("claim.retired.json")
    claim_path.rename(retired)
    shutil.copyfile(retired, claim_path)

    with pytest.raises(RunOwnerFenceSubstituted, match="serialized inode identity"):
        read_run_owner_claim(root, run_id="run-001")
    with pytest.raises(RunOwnerFenceSubstituted, match="serialized inode identity"):
        acquire_run_reconciler_fence(root, prior_owner=claim)


def test_reader_refuses_an_exact_run_tree_copied_to_a_new_run_inode(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ):
        pass

    retired = root.with_name("run-retired")
    root.rename(retired)
    shutil.copytree(retired, root)

    with pytest.raises(RunOwnerFenceSubstituted, match="run root"):
        read_run_owner_claim(root, run_id="run-001")


def test_reader_refuses_a_replaced_runs_parent_with_the_exact_run_moved_back(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ):
        pass

    runs = root.parent
    retired_runs = runs.with_name("runs.retired")
    runs.rename(retired_runs)
    runs.mkdir()
    (retired_runs / root.name).rename(root)

    with pytest.raises(RunOwnerFenceSubstituted, match="runs directory"):
        read_run_owner_claim(root, run_id="run-001")


def test_reader_refuses_a_replaced_shot_root_with_the_exact_runs_tree_moved_back(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ):
        pass

    shot = root.parent.parent
    retired_shot = shot.with_name("shot.retired")
    shot.rename(retired_shot)
    shot.mkdir()
    (retired_shot / "runs").rename(shot / "runs")

    with pytest.raises(RunOwnerFenceSubstituted, match="shot root"):
        read_run_owner_claim(root, run_id="run-001")


def test_live_lease_refuses_a_substituted_canonical_owner_directory(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)
    lease = acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    )
    (root / "owner").rename(root / "owner.retired")
    (root / "owner").mkdir()
    try:
        with pytest.raises(RunOwnerFenceSubstituted, match="owner directory"):
            lease.require_current_identity()
    finally:
        lease.release()


@pytest.mark.parametrize("boundary", ("runs", "shot"))
def test_live_lease_refuses_substituted_namespace_parents_with_exact_children(
    tmp_path: Path,
    boundary: str,
) -> None:
    root, _manifest = _run_root(tmp_path)
    lease = acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    )
    if boundary == "runs":
        runs = root.parent
        retired_runs = runs.with_name("runs.retired")
        runs.rename(retired_runs)
        runs.mkdir()
        (retired_runs / root.name).rename(root)
    else:
        shot = root.parent.parent
        retired_shot = shot.with_name("shot.retired")
        shot.rename(retired_shot)
        shot.mkdir()
        (retired_shot / "runs").rename(shot / "runs")
    try:
        with pytest.raises(RunOwnerFenceSubstituted, match="canonical shot/runs/run namespace"):
            lease.require_current_identity()
    finally:
        lease.release()


@pytest.mark.parametrize("relative_alias", ("claim.alias.json", "fence.alias.lock"))
def test_live_lease_refuses_hard_link_aliases(
    tmp_path: Path,
    relative_alias: str,
) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as lease:
        source = root / (RUN_OWNER_CLAIM if relative_alias.startswith("claim") else RUN_OWNER_FENCE)
        os.link(source, root / "owner" / relative_alias)
        with pytest.raises(RunOwnerFenceError, match="exactly one filesystem name"):
            lease.require_current_identity()


def test_owner_claim_is_create_only_even_after_fence_release(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim_bytes = (root / RUN_OWNER_CLAIM).read_bytes()
        claim = owner.claim

    with pytest.raises(RunOwnerClaimExists, match="only owner-loss reconciliation"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )
    assert (root / RUN_OWNER_CLAIM).read_bytes() == claim_bytes
    with acquire_run_reconciler_fence(root, prior_owner=claim):
        pass


def test_owner_fence_and_claim_publication_fsyncs_in_commit_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _manifest = _run_root(tmp_path)
    real_fsync = os.fsync
    syncs: list[tuple[str, int | None]] = []

    def recording_fsync(descriptor: int) -> None:
        observed = os.fstat(descriptor)
        claim_path = root / RUN_OWNER_CLAIM
        identity = (observed.st_dev, observed.st_ino)
        if stat.S_ISDIR(observed.st_mode):
            if identity == (root.stat().st_dev, root.stat().st_ino):
                syncs.append(("run-root", None))
            elif (root / "owner").exists() and identity == (
                (root / "owner").stat().st_dev,
                (root / "owner").stat().st_ino,
            ):
                syncs.append(
                    (
                        "owner",
                        None if not claim_path.exists() else claim_path.stat().st_nlink,
                    )
                )
        elif (root / RUN_OWNER_FENCE).exists() and identity == (
            (root / RUN_OWNER_FENCE).stat().st_dev,
            (root / RUN_OWNER_FENCE).stat().st_ino,
        ):
            syncs.append(("fence", None))
        else:
            staging = tuple((root / "owner").glob(".claim.tmp.*"))
            if any(identity == (path.stat().st_dev, path.stat().st_ino) for path in staging):
                syncs.append(("claim-staging", None))
        real_fsync(descriptor)

    monkeypatch.setattr(
        "vfx_harness.observability.run_owner_fence.os.fsync",
        recording_fsync,
    )

    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        assert owner.claim.run_id == "run-001"

    assert syncs == [
        ("run-root", None),
        ("fence", None),
        ("owner", None),
        ("claim-staging", None),
        ("owner", 2),
        ("owner", 1),
    ]
    assert (root / RUN_OWNER_CLAIM).stat().st_nlink == 1


@pytest.mark.parametrize("failure_boundary", ("fence", "owner"))
def test_new_fence_durability_failure_refuses_claim_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    root, _manifest = _run_root(tmp_path)
    real_fsync = os.fsync

    def failing_fsync(descriptor: int) -> None:
        observed = os.fstat(descriptor)
        identity = (observed.st_dev, observed.st_ino)
        fence = root / RUN_OWNER_FENCE
        owner = root / "owner"
        if (
            failure_boundary == "fence"
            and fence.exists()
            and identity
            == (
                fence.stat().st_dev,
                fence.stat().st_ino,
            )
        ):
            raise OSError("injected fence fsync failure")
        if (
            failure_boundary == "owner"
            and owner.exists()
            and stat.S_ISDIR(observed.st_mode)
            and identity == (owner.stat().st_dev, owner.stat().st_ino)
            and not (root / RUN_OWNER_CLAIM).exists()
        ):
            raise OSError("injected owner fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(run_owner_fence_module.os, "fsync", failing_fsync)

    with pytest.raises(RunOwnerFenceError, match=r"durably (initialize|publish)"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert not (root / RUN_OWNER_CLAIM).exists()


def test_claim_reader_refuses_an_injected_hard_link_alias(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim = owner.claim

    alias = root / "owner" / "claim.alias.json"
    os.link(root / RUN_OWNER_CLAIM, alias)
    assert (root / RUN_OWNER_CLAIM).stat().st_nlink == 2

    with pytest.raises(RunOwnerFenceError, match="exactly one filesystem name"):
        read_run_owner_claim(root, run_id="run-001")
    with pytest.raises(RunOwnerFenceError, match="exactly one filesystem name"):
        acquire_run_reconciler_fence(root, prior_owner=claim)


def test_crash_between_claim_link_and_alias_unlink_is_not_publishable(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)
    script = """
import os
import sys
import vfx_harness.observability.run_owner_fence as owner_fence

real_exit = os._exit
real_unlink = os.unlink

def crash_before_alias_unlink(path, *args, **kwargs):
    if str(path).startswith(".claim.tmp."):
        real_exit(77)
    return real_unlink(path, *args, **kwargs)

owner_fence.os.unlink = crash_before_alias_unlink
owner_fence.acquire_run_owner_fence(
    sys.argv[1], run_id=sys.argv[2], command="plan", owner_kind="direct"
)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(root), "run-001"],
        capture_output=True,
        text=True,
        env=_child_environment(),
        timeout=5,
        check=False,
    )

    assert process.returncode == 77, (process.stdout, process.stderr)
    aliases = tuple((root / "owner").glob(".claim.tmp.*"))
    assert len(aliases) == 1
    assert (root / RUN_OWNER_CLAIM).stat().st_nlink == 2
    assert aliases[0].stat().st_ino == (root / RUN_OWNER_CLAIM).stat().st_ino
    with pytest.raises(RunOwnerFenceError, match="exactly one filesystem name"):
        read_run_owner_claim(root, run_id="run-001")


def test_manifest_or_claim_tampering_refuses_reconciliation(tmp_path: Path) -> None:
    root, manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim = owner.claim

    manifest["invocation"] = {
        "dispatch": {
            "schema": RUN_DISPATCH_SCHEMA,
            "kind": "driver",
            "command": "run",
        },
        "argv": ["vfx run", "different"],
        "parameters": {},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RunOwnerFenceError, match="manifest digest is stale"):
        acquire_run_reconciler_fence(root, prior_owner=claim)

    (root / "manifest.json").write_text(
        json.dumps(
            {
                **manifest,
                "invocation": {
                    "dispatch": {
                        "schema": RUN_DISPATCH_SCHEMA,
                        "kind": "direct",
                        "command": "plan",
                    },
                    "argv": ["vfx plan", "fixture"],
                    "parameters": {},
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    raw_claim = json.loads((root / RUN_OWNER_CLAIM).read_text(encoding="utf-8"))
    raw_claim["unexpected"] = True
    (root / RUN_OWNER_CLAIM).write_text(
        json.dumps(raw_claim, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RunOwnerFenceError, match="fields mismatch"):
        acquire_run_reconciler_fence(root, prior_owner=claim)


@pytest.mark.parametrize("encoding", ("utf-16", "utf-32"))
def test_claim_reader_and_reconciler_refuse_a_non_utf8_claim(
    tmp_path: Path,
    encoding: str,
) -> None:
    root, _manifest = _run_root(tmp_path)
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ) as owner:
        claim = owner.claim
        claim_record = json.loads((root / RUN_OWNER_CLAIM).read_bytes())

    (root / RUN_OWNER_CLAIM).write_bytes((json.dumps(claim_record, indent=2, sort_keys=True) + "\n").encode(encoding))
    with pytest.raises(RunOwnerFenceError, match="strict JSON object"):
        read_run_owner_claim(root, run_id="run-001")
    with pytest.raises(RunOwnerFenceError, match="strict JSON object"):
        acquire_run_reconciler_fence(root, prior_owner=claim)


def test_owner_requires_exact_real_run_layout_and_regular_fence(tmp_path: Path) -> None:
    wrong = tmp_path / "run-001"
    wrong.mkdir()
    (wrong / "manifest.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(RunOwnerFenceError, match="exact <shot>/runs"):
        acquire_run_owner_fence(
            wrong,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    root, _manifest = _run_root(tmp_path, "run-002")
    fence = root / RUN_OWNER_FENCE
    fence.parent.mkdir()
    fence.mkdir()
    with pytest.raises(RunOwnerFenceError, match="real regular file"):
        acquire_run_owner_fence(
            root,
            run_id="run-002",
            command="plan",
            owner_kind="direct",
        )


@pytest.mark.parametrize("failure_boundary", ("run root", "run owner directory"))
def test_directory_setup_failure_closes_every_acquisition_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    root, _manifest = _run_root(tmp_path)
    descriptor_directory = Path("/proc/self/fd")
    if not descriptor_directory.is_dir():
        pytest.skip("descriptor accounting fixture requires procfs")
    before = len(tuple(descriptor_directory.iterdir()))
    real_set_non_inheritable = run_owner_fence_files_module._set_non_inheritable

    def fail_owner_descriptor(descriptor: int, *, where: str) -> None:
        if where == failure_boundary:
            raise RunOwnerFenceError("injected descriptor setup failure")
        real_set_non_inheritable(descriptor, where=where)

    monkeypatch.setattr(
        run_owner_fence_files_module,
        "_set_non_inheritable",
        fail_owner_descriptor,
    )
    with pytest.raises(RunOwnerFenceError, match="injected descriptor"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert len(tuple(descriptor_directory.iterdir())) == before


def test_process_death_releases_exact_fence_for_reconciler(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    script = """
import os
import sys
from vfx_harness.observability.run_owner_fence import acquire_run_owner_fence

lease = acquire_run_owner_fence(
    sys.argv[1], run_id=sys.argv[2], command="plan", owner_kind="direct"
)
print(lease.claim.digest, flush=True)
sys.stdin.readline()
os._exit(23)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), "run-001"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    assert process.stdout is not None
    digest = process.stdout.readline().strip()
    claim = read_run_owner_claim(
        root,
        run_id="run-001",
        expected_digest=digest,
    )
    try:
        with pytest.raises(RunOwnerFenceActive, match="live root invocation"):
            acquire_run_reconciler_fence(root, prior_owner=claim)
    finally:
        stdout, stderr = process.communicate("crash\n", timeout=5)

    assert process.returncode == 23, (stdout, stderr)
    with acquire_run_reconciler_fence(root, prior_owner=claim) as reconciler:
        assert reconciler.fence_identity == (
            claim.fence_device,
            claim.fence_inode,
        )


def test_fork_child_cannot_release_the_live_parent_lease(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    lease = acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    )
    child = os.fork()
    if child == 0:
        try:
            lease.release()
        except RunOwnerFenceError:
            os._exit(0)
        os._exit(91)

    _pid, wait_status = os.waitpid(child, 0)
    try:
        assert os.waitstatus_to_exitcode(wait_status) == 0
        with pytest.raises(RunOwnerFenceActive, match="live root invocation"):
            acquire_run_reconciler_fence(root, prior_owner=lease.claim)
    finally:
        lease.release()


def test_release_keeps_registry_visible_through_close_and_closes_on_registry_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _manifest = _run_root(tmp_path)
    lease = acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    )
    token = lease._registry_token
    claim = lease.claim
    real_close = run_owner_fence_module._close_acquisition

    def asserting_close(**kwargs: object) -> None:
        assert token in run_owner_fork_guard_module._ACTIVE_DESCRIPTORS
        real_close(**kwargs)

    monkeypatch.setattr(run_owner_fence_module, "_close_acquisition", asserting_close)
    run_owner_fork_guard_module._ACTIVE_DESCRIPTORS.pop(token)

    with pytest.raises(RunOwnerFenceError, match="absent from the fork descriptor registry"):
        lease.release()

    assert token not in run_owner_fork_guard_module._ACTIVE_DESCRIPTORS
    monkeypatch.setattr(run_owner_fence_module, "_close_acquisition", real_close)
    with acquire_run_reconciler_fence(root, prior_owner=claim):
        pass


@pytest.mark.parametrize("phase", ("open-before-lock", "lock-before-lease"))
def test_prelease_fork_never_leaves_a_descendant_holding_the_owner_ofd(
    tmp_path: Path,
    phase: str,
) -> None:
    root, _manifest = _run_root(tmp_path)
    script = r"""
import os
import sys
import time
import vfx_harness.observability.run_owner_fence as owner_fence

phase = sys.argv[3]
descendant = None

def fork_sleeper():
    child = os.fork()
    if child == 0:
        os.close(1)
        os.close(2)
        time.sleep(30)
        os._exit(0)
    return child

if phase == "open-before-lock":
    real_acquire = owner_fence._acquire_exclusive
    def injected_acquire(descriptor, *, path):
        global descendant
        descendant = fork_sleeper()
        return real_acquire(descriptor, path=path)
    owner_fence._acquire_exclusive = injected_acquire
else:
    real_post_init = owner_fence.RunOwnerFenceLease.__post_init__
    def injected_post_init(self):
        global descendant
        if self.acquisition_kind == "owner" and descendant is None:
            descendant = fork_sleeper()
        return real_post_init(self)
    owner_fence.RunOwnerFenceLease.__post_init__ = injected_post_init

lease = owner_fence.acquire_run_owner_fence(
    sys.argv[1], run_id=sys.argv[2], command="plan", owner_kind="direct"
)
print(lease.claim.digest, descendant, flush=True)
os._exit(27)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(root), "run-001", phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    assert owner.stdout is not None
    digest, descendant_pid_raw = owner.stdout.readline().strip().split()
    descendant_pid = int(descendant_pid_raw)
    _stdout, stderr = owner.communicate(timeout=5)
    assert owner.returncode == 27, stderr
    claim = read_run_owner_claim(root, run_id="run-001", expected_digest=digest)

    try:
        os.kill(descendant_pid, 0)
        with acquire_run_reconciler_fence(root, prior_owner=claim):
            os.kill(descendant_pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.kill(descendant_pid, signal.SIGTERM)


def test_fork_only_descendant_does_not_retain_the_dead_owner_fence(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)
    script = """
import os
import sys
import time
from vfx_harness.observability.run_owner_fence import acquire_run_owner_fence

lease = acquire_run_owner_fence(
    sys.argv[1], run_id=sys.argv[2], command="plan", owner_kind="direct"
)
descendant = os.fork()
if descendant == 0:
    os.close(1)
    os.close(2)
    time.sleep(30)
    os._exit(0)
print(lease.claim.digest, descendant, flush=True)
os._exit(25)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(root), "run-001"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    assert owner.stdout is not None
    digest, descendant_pid_raw = owner.stdout.readline().strip().split()
    descendant_pid = int(descendant_pid_raw)
    _stdout, stderr = owner.communicate(timeout=5)
    assert owner.returncode == 25, stderr
    claim = read_run_owner_claim(root, run_id="run-001", expected_digest=digest)

    try:
        os.kill(descendant_pid, 0)
        with acquire_run_reconciler_fence(root, prior_owner=claim):
            os.kill(descendant_pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.kill(descendant_pid, signal.SIGTERM)


def test_exec_descendant_cannot_inherit_fence_after_owner_exits(tmp_path: Path) -> None:
    root, _manifest = _run_root(tmp_path)
    script = """
import os
import subprocess
import sys
from vfx_harness.observability.run_owner_fence import acquire_run_owner_fence

lease = acquire_run_owner_fence(
    sys.argv[1], run_id=sys.argv[2], command="plan", owner_kind="direct"
)
descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=False,
)
print(lease.claim.digest, descendant.pid, flush=True)
os._exit(24)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(root), "run-001"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    assert owner.stdout is not None
    digest, descendant_pid_raw = owner.stdout.readline().strip().split()
    descendant_pid = int(descendant_pid_raw)
    _stdout, stderr = owner.communicate(timeout=5)
    assert owner.returncode == 24, stderr
    claim = read_run_owner_claim(
        root,
        run_id="run-001",
        expected_digest=digest,
    )

    try:
        os.kill(descendant_pid, 0)
        with acquire_run_reconciler_fence(root, prior_owner=claim) as reconciler:
            assert reconciler.fence_identity == (
                claim.fence_device,
                claim.fence_inode,
            )
            os.kill(descendant_pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.kill(descendant_pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and Path(f"/proc/{descendant_pid}").exists():
            time.sleep(0.01)


def test_managed_fork_guard_entry_failure_does_not_pin_run_owner_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _manifest = _run_root(tmp_path)
    original_managed = run_owner_fence_module.managed_fork_protected_acquisition

    @contextmanager
    def enter_then_interrupt():
        with original_managed():
            raise RuntimeError("injected run-owner managed-entry interruption")
            yield  # pragma: no cover - required generator shape

    monkeypatch.setattr(
        run_owner_fence_module,
        "managed_fork_protected_acquisition",
        enter_then_interrupt,
    )
    with pytest.raises(RuntimeError, match="managed-entry interruption"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert run_owner_fork_guard_module._PENDING_DESCRIPTORS == {}
    assert run_owner_fork_guard_module._ACTIVE_DESCRIPTORS == {}
    monkeypatch.setattr(
        run_owner_fence_module,
        "managed_fork_protected_acquisition",
        original_managed,
    )
    with acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    ):
        pass


def test_run_owner_handoff_interruption_repairs_guard_for_reconciler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _manifest = _run_root(tmp_path)

    class InterruptAfterFirstInsert(dict):
        interrupted = False

        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if not self.interrupted:
                self.interrupted = True
                raise RuntimeError("injected run-owner active handoff interruption")

    active = InterruptAfterFirstInsert(
        run_owner_fork_guard_module._ACTIVE_DESCRIPTORS
    )
    monkeypatch.setattr(
        run_owner_fork_guard_module,
        "_ACTIVE_DESCRIPTORS",
        active,
    )
    with pytest.raises(RuntimeError, match="active handoff interruption"):
        acquire_run_owner_fence(
            root,
            run_id="run-001",
            command="plan",
            owner_kind="direct",
        )

    assert run_owner_fork_guard_module._PENDING_DESCRIPTORS == {}
    assert run_owner_fork_guard_module._ACTIVE_DESCRIPTORS == {}
    claim = read_run_owner_claim(root, run_id="run-001")
    with acquire_run_reconciler_fence(root, prior_owner=claim):
        pass


def test_run_owner_release_skips_closed_and_reused_fence_descriptor(
    tmp_path: Path,
) -> None:
    root, _manifest = _run_root(tmp_path)
    lease = acquire_run_owner_fence(
        root,
        run_id="run-001",
        command="plan",
        owner_kind="direct",
    )
    original_fence_descriptor = lease._fence_descriptor
    os.close(original_fence_descriptor)
    reused = os.open(os.devnull, os.O_RDONLY)
    assert reused == original_fence_descriptor
    try:
        with pytest.raises(
            RunOwnerFenceSubstituted,
            match="changed identity before release",
        ):
            lease.release()
        os.fstat(reused)
        child = os.fork()
        if child == 0:
            try:
                os.fstat(reused)
            except OSError:
                os._exit(91)
            os._exit(0)
        _pid, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(reused)

    assert run_owner_fork_guard_module._ACTIVE_DESCRIPTORS == {}
    with acquire_run_reconciler_fence(root, prior_owner=lease.claim):
        pass
