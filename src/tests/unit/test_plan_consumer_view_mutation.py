"""Opaque allocation and mutation authority for isolated plan-consumer views."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import pytest

from vfx_harness.domain.authority_head_records import (
    OVERLAY_ARTIFACTS,
    canonical_json_bytes,
)
from vfx_harness.observability import run_owner_fork_guard, run_owner_fork_registry
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import (
    plan_consumer_owned_directory,
    plan_consumer_view_allocation,
    plan_consumer_view_cleanup,
    plan_consumer_view_installation,
    plan_consumer_view_lifecycle,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.plan_consumer_ledger_projection import (
    update_plan_consumer_ledger,
)
from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
)
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    PlanConsumerViewMutationConflict,
    PreparedPlanConsumerViewAllocation,
    allocating_plan_consumer_view,
    constructing_plan_consumer_view,
    discard_plan_consumer_view_allocation,
    discard_prepared_plan_consumer_view_installation,
    install_plan_consumer_view,
    mutating_plan_consumer_view,
    prepare_plan_consumer_view_installation,
    require_plan_consumer_view_mutation,
)
from vfx_harness.orchestration.plan_consumer_view_projection import (
    create_construction_ledger_snapshot,
)

_ALLOCATION_CONTEXTS: list[AbstractContextManager[Any]] = []
_ALLOCATION_CONTEXT_LOCK = Lock()


@pytest.fixture(autouse=True)
def _close_test_allocation_scopes() -> None:
    yield
    with _ALLOCATION_CONTEXT_LOCK:
        contexts = tuple(reversed(_ALLOCATION_CONTEXTS))
        _ALLOCATION_CONTEXTS.clear()
    for context in contexts:
        context.__exit__(None, None, None)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _directory_identity(path: Path) -> PlanConsumerDirectoryIdentity:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return PlanConsumerDirectoryIdentity.capture(descriptor)
    finally:
        os.close(descriptor)


def _layout(root: Path, run_id: str = "consumer") -> RunLayout:
    shot = root / "shot"
    run = shot / "runs" / run_id
    (run / "scratch").mkdir(parents=True)
    return RunLayout(shot=shot, run_id=run_id, root=run)


def _sibling_layout(layout: RunLayout, run_id: str) -> RunLayout:
    run = layout.shot / "runs" / run_id
    (run / "scratch").mkdir(parents=True)
    return RunLayout(shot=layout.shot, run_id=run_id, root=run)


def _marker(shot: Path) -> PlanConsumerViewMarker:
    content_hash = _digest("bundle")
    return PlanConsumerViewMarker(
        shot=shot,
        bundle=(
            shot
            / "runs"
            / "publisher"
            / "checkpoints"
            / "plans"
            / "bundles"
            / content_hash
        ),
        content_hash=content_hash,
        base_selection=AuthoritySelectionToken(0, None, 0, None),
        view_source="bundle",
        view_digest=content_hash,
        artifact_hashes={name: _digest(name) for name in OVERLAY_ARTIFACTS},
        authored_inputs={},
        decision_inputs={},
    )


def _write_marker(root: Path, marker: PlanConsumerViewMarker) -> None:
    (root / ".plan-consumer-view.json").write_bytes(
        canonical_json_bytes(marker.to_dict())
    )


def _allocate(
    layout: RunLayout,
    marker: PlanConsumerViewMarker,
) -> tuple[Path, PreparedPlanConsumerViewAllocation]:
    context = allocating_plan_consumer_view(layout)
    view, allocation = context.__enter__()
    with _ALLOCATION_CONTEXT_LOCK:
        _ALLOCATION_CONTEXTS.append(context)
    return view, allocation


def _install(layout: RunLayout, marker: PlanConsumerViewMarker) -> Path:
    _temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)
    installed = layout.scratch / "plan-consumer-view"
    return install_plan_consumer_view(installation, installed)


def test_caller_created_directory_and_forged_receipt_cannot_construct(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    caller_view = layout.scratch / ".plan-consumer-view.tmp-caller"
    caller_view.mkdir()
    _write_marker(caller_view, marker)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="exact opaque allocation receipt",
    ), constructing_plan_consumer_view(
        layout,
        caller_view,  # type: ignore[arg-type]
        marker,
    ):
        pytest.fail("a caller-created directory minted construction authority")

    forged = object.__new__(PreparedPlanConsumerViewAllocation)
    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="unregistered, expired, consumed, or forked",
    ), constructing_plan_consumer_view(layout, forged, marker):
        pytest.fail("a forged allocation receipt minted construction authority")

    assert caller_view.is_dir()
    assert (caller_view / ".plan-consumer-view.json").is_file()


def test_allocation_receipt_is_layout_scoped_and_one_shot(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    other_layout = _sibling_layout(layout, "other-consumer")
    marker = _marker(layout.shot)
    temporary, allocation = _allocate(layout, marker)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="another layout, process, or thread",
    ), constructing_plan_consumer_view(other_layout, allocation, marker):
        pytest.fail("another layout claimed the allocation")

    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == temporary

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="unregistered, expired, consumed, or forked",
    ), constructing_plan_consumer_view(layout, allocation, marker):
        pytest.fail("a consumed allocation receipt was reused")


def test_allocation_receipt_is_thread_scoped(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    temporary, allocation = _allocate(layout, marker)
    failures: list[BaseException] = []

    def claim_from_another_thread() -> None:
        try:
            with constructing_plan_consumer_view(layout, allocation, marker):
                pytest.fail("another thread claimed the allocation")
        except BaseException as exc:  # asserted below
            failures.append(exc)

    thread = Thread(target=claim_from_another_thread)
    thread.start()
    thread.join(5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], PlanConsumerViewMutationConflict)
    assert "another layout, process, or thread" in str(failures[0])
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == temporary


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_allocation_receipt_is_fork_scoped(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    temporary, allocation = _allocate(layout, marker)
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions execute in the parent
        os.close(read_descriptor)
        result = b"wrong-error"
        try:
            with constructing_plan_consumer_view(layout, allocation, marker):
                result = b"admitted"
        except PlanConsumerViewMutationConflict:
            result = b"refused"
        os.write(write_descriptor, result)
        os.close(write_descriptor)
        os._exit(0)

    os.close(write_descriptor)
    child_result = os.read(read_descriptor, 32)
    os.close(read_descriptor)
    waited, status = os.waitpid(child, 0)

    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert child_result == b"refused"
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == temporary


def test_live_root_identical_copy_and_cross_shot_view_cannot_mint_mutation(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    _write_marker(layout.shot, marker)
    copied = layout.scratch / "byte-identical-copy"
    shutil.copytree(installed, copied)
    other_shot = tmp_path / "other-shot"
    other_shot.mkdir()

    for shot, view in (
        (layout.shot, layout.shot),
        (layout.shot, copied),
        (other_shot, installed),
    ):
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match=r"not installed|another canonical shot",
        ), mutating_plan_consumer_view(shot, view, marker):
            pytest.fail("an unregistered or cross-shot view minted mutation authority")

    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed
    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="not installed",
    ), mutating_plan_consumer_view(layout.shot, installed, marker):
        pytest.fail("an installed registration was consumed twice")


def test_installed_registration_is_thread_scoped_and_preserved_on_refusal(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    failures: list[BaseException] = []

    def mutate_from_another_thread() -> None:
        try:
            with mutating_plan_consumer_view(layout.shot, installed, marker):
                pytest.fail("another thread claimed the installed view")
        except BaseException as exc:  # asserted below
            failures.append(exc)

    thread = Thread(target=mutate_from_another_thread)
    thread.start()
    thread.join(5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], PlanConsumerViewMutationConflict)
    assert "another generation" in str(failures[0])
    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed


def test_hostile_final_destination_equal_to_shot_refuses_before_rename(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hostile-run"
    shot = root / "scratch" / "plan-consumer-view"
    shot.mkdir(parents=True)
    sentinel = shot / "canonical-sentinel.txt"
    sentinel.write_text("preserve canonical shot\n", encoding="utf-8")
    layout = RunLayout(shot=shot, run_id="hostile", root=root)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="exact canonical",
    ), allocating_plan_consumer_view(layout):
        pytest.fail("a hostile layout allocated a consumer view")

    assert shot.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "preserve canonical shot\n"


@pytest.mark.parametrize("component", ["shot", "scratch"])
def test_source_substitution_between_construction_and_install_refuses(
    tmp_path: Path,
    component: str,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)

    target = layout.shot if component == "shot" else layout.scratch
    backup = target.with_name(target.name + "-original")
    target.rename(backup)
    substitute_scratch = (
        target / "runs" / layout.run_id / "scratch"
        if component == "shot"
        else target
    )
    substitute_scratch.mkdir(parents=True)
    sentinel = substitute_scratch / "foreign-generation.bin"
    sentinel.write_bytes(b"preserve-substitute\x00")

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="source shot or scratch generation changed",
    ):
        install_plan_consumer_view(
            installation,
            substitute_scratch / "plan-consumer-view",
        )

    assert sentinel.read_bytes() == b"preserve-substitute\x00"
    assert not (
        substitute_scratch / ".plan-consumer-view.transaction.lock"
    ).exists()
    shutil.rmtree(target)
    backup.rename(target)
    assert temporary.is_dir()
    discard_prepared_plan_consumer_view_installation(installation)


@pytest.mark.parametrize("component", ["shot", "scratch"])
def test_source_substitution_between_install_and_mutation_refuses(
    tmp_path: Path,
    component: str,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    target = layout.shot if component == "shot" else layout.scratch
    backup = target.with_name(target.name + "-original")
    target.rename(backup)
    substitute_scratch = (
        target / "runs" / layout.run_id / "scratch"
        if component == "shot"
        else target
    )
    substitute_scratch.mkdir(parents=True)
    sentinel = substitute_scratch / "foreign-generation.bin"
    sentinel.write_bytes(b"preserve-substitute\x00")

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="source shot or scratch generation changed",
    ), mutating_plan_consumer_view(layout.shot, installed, marker):
        pytest.fail("a substituted source generation minted mutation authority")

    assert sentinel.read_bytes() == b"preserve-substitute\x00"
    assert not (
        substitute_scratch / ".plan-consumer-view.transaction.lock"
    ).exists()
    shutil.rmtree(target)
    backup.rename(target)
    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed


def test_ledger_binding_rejects_external_replacement_and_typed_update_advances(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    _temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        create_construction_ledger_snapshot(capability, b'{"revision":1}\n')
        installation = prepare_plan_consumer_view_installation(capability)
    installed = install_plan_consumer_view(
        installation,
        layout.scratch / "plan-consumer-view",
    )
    ledger = installed / "shot.json"
    original = ledger.read_bytes()
    replacement = installed / "replacement-ledger.json"
    replacement.write_bytes(original)
    os.replace(replacement, ledger)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="registration differs",
    ), mutating_plan_consumer_view(layout.shot, installed, marker):
        pytest.fail("a byte-identical replacement ledger retained authority")

    # A fresh registered generation proves that its typed CAS can advance both
    # the ledger inode and digest binding.
    second_layout = _sibling_layout(layout, "typed-ledger-update")
    second_marker = _marker(second_layout.shot)
    _second_temporary, second_allocation = _allocate(second_layout, second_marker)
    with constructing_plan_consumer_view(
        second_layout,
        second_allocation,
        second_marker,
    ) as capability:
        create_construction_ledger_snapshot(capability, b'{"revision":1}\n')
        second_installation = prepare_plan_consumer_view_installation(capability)
    second = install_plan_consumer_view(
        second_installation,
        second_layout.scratch / "plan-consumer-view",
    )
    with mutating_plan_consumer_view(
        second_layout.shot,
        second,
        second_marker,
    ) as capability:
        _result, digest = update_plan_consumer_ledger(
            capability,
            lambda _current: (b'{"revision":2}\n', "advanced"),
        )
        assert digest == hashlib.sha256(b'{"revision":2}\n').hexdigest()
        assert require_plan_consumer_view_mutation(capability) == second
    assert (second / "shot.json").read_bytes() == b'{"revision":2}\n'


def test_installed_ledger_cas_cannot_populate_a_construction_view(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    _temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        create_construction_ledger_snapshot(capability, b'{"revision":1}\n')
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="installed capability",
        ):
            update_plan_consumer_ledger(
                capability,
                lambda current: (current, None),
            )


def test_allocation_cleanup_refuses_substitute_and_preserves_its_bytes(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    temporary, allocation = _allocate(layout, _marker(layout.shot))
    original = temporary.with_name(temporary.name + "-original")
    temporary.rename(original)
    temporary.mkdir()
    sentinel = temporary / "foreign.bin"
    sentinel.write_bytes(b"do-not-delete\x00")

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="substituted",
    ):
        discard_plan_consumer_view_allocation(layout, allocation)

    assert sentinel.read_bytes() == b"do-not-delete\x00"
    shutil.rmtree(temporary)
    original.rename(temporary)
    discard_plan_consumer_view_allocation(layout, allocation)


def test_allocation_scope_owns_cleanup_across_return_assignment_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    original_allocate = plan_consumer_view_allocation._allocate_plan_consumer_view

    def allocate_then_interrupt(
        observed_layout: RunLayout,
        scope_token: object,
    ) -> tuple[Path, PreparedPlanConsumerViewAllocation]:
        original_allocate(observed_layout, scope_token)
        raise KeyboardInterrupt("injected after allocation return effect")

    monkeypatch.setattr(
        plan_consumer_view_allocation,
        "_allocate_plan_consumer_view",
        allocate_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt, match="allocation return effect"), allocating_plan_consumer_view(layout):
        pytest.fail("allocation result was unexpectedly yielded")

    assert not list(layout.scratch.glob(".plan-consumer-view.tmp-*"))
    assert len(list(layout.scratch.glob(".plan-consumer-view.retired-*"))) == 1


def test_allocation_failure_before_identity_capture_preserves_unknown_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    original_open_beneath = plan_consumer_owned_directory.open_beneath_directory
    failed = False

    def refuse_created_child_open(parent_descriptor: int, name: str) -> int:
        nonlocal failed
        if not failed and name.startswith(".plan-consumer-directory.tmp-"):
            failed = True
            raise OSError(
                errno.ENOENT,
                "injected failure before allocation identity capture",
            )
        return original_open_beneath(parent_descriptor, name)

    monkeypatch.setattr(
        plan_consumer_owned_directory,
        "open_beneath_directory",
        refuse_created_child_open,
    )
    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="before descriptor capture",
    ), allocating_plan_consumer_view(layout):
        pytest.fail("uncaptured allocation was unexpectedly yielded")

    assert failed
    # The created inode was never proven ours, so it is preserved, not deleted,
    # and the final view name never appeared.
    preserved = list(layout.scratch.glob(".plan-consumer-directory.tmp-*"))
    assert len(preserved) == 1
    assert preserved[0].is_dir()
    assert list(layout.scratch.glob(".plan-consumer-view.tmp-*")) == []


def test_forged_rename_receipt_cannot_delete_named_directory(tmp_path: Path) -> None:
    target = tmp_path / "must-survive"
    target.mkdir()
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"opaque-receipt-required\x00")
    forged = object.__new__(
        plan_consumer_view_installation.PlanConsumerViewInstallationRename
    )

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="forged, expired, consumed, or forked",
    ):
        plan_consumer_view_installation._discard_previous_plan_consumer_view(
            forged,
            complete_registration=lambda: None,
        )

    assert sentinel.read_bytes() == b"opaque-receipt-required\x00"


def test_second_rename_failure_restores_predecessor_and_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    predecessor_identity = installed.stat().st_ino
    sentinel = installed / "predecessor.txt"
    sentinel.write_text("preserve predecessor\n", encoding="utf-8")
    temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)

    original_move = plan_consumer_view_installation.move_owned_directory_noreplace

    def fail_temporary_install(
        parent_descriptor: int,
        source: str,
        destination: str,
        expected: object,
        *,
        where: str,
    ) -> None:
        if source == temporary.name and destination == installed.name:
            raise OSError("injected second rename failure")
        original_move(
            parent_descriptor,
            source,
            destination,
            expected,  # type: ignore[arg-type]
            where=where,
        )

    monkeypatch.setattr(
        plan_consumer_view_installation,
        "move_owned_directory_noreplace",
        fail_temporary_install,
    )
    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="prior generation was restored",
    ):
        install_plan_consumer_view(installation, installed)

    assert installed.stat().st_ino == predecessor_identity
    assert sentinel.read_text(encoding="utf-8") == "preserve predecessor\n"
    assert temporary.is_dir()
    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed


def test_failed_install_recovery_swap_never_certifies_a_foreign_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    installed = scratch / "plan-consumer-view"
    previous = scratch / ".plan-consumer-view.previous-test"
    temporary = scratch / ".plan-consumer-view.tmp-test"
    installed.mkdir()
    previous.mkdir()
    installed_identity = _directory_identity(installed)
    previous_identity = _directory_identity(previous)
    transaction = plan_consumer_view_installation._InstallationRenameRecord(
        scratch=scratch,
        temporary=temporary,
        installed=installed,
        installed_identity=installed_identity,
        previous=previous,
        previous_identity=previous_identity,
    )
    original_rename = plan_consumer_view_cleanup._rename_child_noreplace
    held_new = scratch / "held-exact-new"
    foreign_identity: PlanConsumerDirectoryIdentity | None = None
    swapped = False

    def swap_before_recovery_rename(
        parent_descriptor: int,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal foreign_identity, swapped
        if source_name == installed.name and not swapped:
            swapped = True
            os.rename(
                source_name,
                held_new.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.mkdir(source_name, dir_fd=parent_descriptor)
            foreign_identity = _directory_identity(installed)
        original_rename(parent_descriptor, source_name, destination_name)

    monkeypatch.setattr(
        plan_consumer_view_cleanup,
        "_rename_child_noreplace",
        swap_before_recovery_rename,
    )
    scratch_descriptor = os.open(
        scratch,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="could not prove that the exact source generation moved",
        ):
            plan_consumer_view_installation._recover_failed_begin(
                transaction,
                scratch_descriptor,
            )

        assert swapped
        assert _directory_identity(held_new) == installed_identity
        assert _directory_identity(temporary) == foreign_identity
        assert _directory_identity(previous) == previous_identity
        assert not plan_consumer_view_installation._RENAMES

        monkeypatch.setattr(
            plan_consumer_view_cleanup,
            "_rename_child_noreplace",
            original_rename,
        )
        shutil.rmtree(temporary)
        held_new.rename(installed)
        plan_consumer_view_installation._recover_failed_begin(
            transaction,
            scratch_descriptor,
        )
        assert _directory_identity(temporary) == installed_identity
        assert _directory_identity(installed) == previous_identity
    finally:
        os.close(scratch_descriptor)


def test_registered_install_recovers_exact_pending_predecessor_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    _install(layout, marker)
    _temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)

    original_retire = plan_consumer_view_installation.retire_owned_directory
    interrupted = False

    def interrupt_predecessor_cleanup(
        parent_descriptor: int,
        source_name: str,
        expected: object,
    ) -> None:
        nonlocal interrupted
        if source_name.startswith(".plan-consumer-view.previous-") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected before predecessor retirement")
        original_retire(
            parent_descriptor,
            source_name,
            expected,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        plan_consumer_view_installation,
        "retire_owned_directory",
        interrupt_predecessor_cleanup,
    )
    installed = layout.scratch / "plan-consumer-view"
    with pytest.raises(KeyboardInterrupt, match="before predecessor retirement"):
        install_plan_consumer_view(installation, installed)
    assert interrupted
    assert list(layout.scratch.glob(".plan-consumer-view.previous-*"))

    monkeypatch.setattr(
        plan_consumer_view_installation,
        "retire_owned_directory",
        original_retire,
    )
    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed
    assert not list(layout.scratch.glob(".plan-consumer-view.previous-*"))
    assert list(layout.scratch.glob(".plan-consumer-view.retired-*"))


def test_post_retirement_interruption_preserves_commit_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    _install(layout, marker)
    _temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)

    original_retire = plan_consumer_view_installation.retire_owned_directory
    interrupted = False

    def retire_then_interrupt(
        parent_descriptor: int,
        source_name: str,
        expected: object,
    ) -> None:
        nonlocal interrupted
        original_retire(
            parent_descriptor,
            source_name,
            expected,  # type: ignore[arg-type]
        )
        if source_name.startswith(".plan-consumer-view.previous-") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after predecessor retirement")

    monkeypatch.setattr(
        plan_consumer_view_installation,
        "retire_owned_directory",
        retire_then_interrupt,
    )
    installed = layout.scratch / "plan-consumer-view"
    with pytest.raises(KeyboardInterrupt, match="after predecessor retirement"):
        install_plan_consumer_view(installation, installed)
    assert interrupted
    assert not list(layout.scratch.glob(".plan-consumer-view.previous-*"))
    assert list(layout.scratch.glob(".plan-consumer-view.retired-*"))

    monkeypatch.setattr(
        plan_consumer_view_installation,
        "retire_owned_directory",
        original_retire,
    )

    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed


def test_interruption_after_install_registration_propagates_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    _temporary, allocation = _allocate(layout, marker)
    with constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)

    installed = layout.scratch / "plan-consumer-view"
    key = (layout.shot.absolute(), installed.absolute())
    original_block = plan_consumer_view_lifecycle.block_deferred_signals
    interrupted = False

    @contextmanager
    def interrupt_after_committed_registration() -> Iterator[None]:
        nonlocal interrupted
        with original_block():
            yield
        if (
            not interrupted
            and key in plan_consumer_view_lifecycle._registry.INSTALLED
            and id(installation)
            not in plan_consumer_view_lifecycle._registry.INSTALLATIONS
        ):
            interrupted = True
            raise KeyboardInterrupt("injected after committed install registration")

    monkeypatch.setattr(
        plan_consumer_view_lifecycle,
        "block_deferred_signals",
        interrupt_after_committed_registration,
    )
    with pytest.raises(
        KeyboardInterrupt,
        match="after committed install registration",
    ):
        install_plan_consumer_view(installation, installed)

    assert interrupted
    monkeypatch.setattr(
        plan_consumer_view_lifecycle,
        "block_deferred_signals",
        original_block,
    )
    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed


def test_concurrent_installers_share_one_exclusive_physical_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    first_entered = Event()
    release_first = Event()
    original_install = (
        plan_consumer_view_lifecycle._install_and_verify_allocated_plan_consumer_view
    )
    calls = 0
    call_lock = Lock()

    def pause_first_install(**kwargs: object) -> object:
        nonlocal calls

        with call_lock:
            calls += 1
            current = calls
        if current == 1:
            first_entered.set()
            assert release_first.wait(5)
        return original_install(**kwargs)

    monkeypatch.setattr(
        plan_consumer_view_lifecycle,
        "_install_and_verify_allocated_plan_consumer_view",
        pause_first_install,
    )
    results: list[Path] = []
    failures: list[BaseException] = []

    def install_generation() -> None:
        proof = None
        try:
            _temporary, allocation = _allocate(layout, marker)
            with constructing_plan_consumer_view(
                layout,
                allocation,
                marker,
            ) as capability:
                proof = prepare_plan_consumer_view_installation(capability)
            results.append(
                install_plan_consumer_view(
                    proof,
                    layout.scratch / "plan-consumer-view",
                )
            )
            proof = None
        except BaseException as exc:  # asserted below
            failures.append(exc)
        finally:
            if proof is not None:
                discard_prepared_plan_consumer_view_installation(proof)

    first = Thread(target=install_generation)
    first.start()
    assert first_entered.wait(5)
    second = Thread(target=install_generation)
    second.start()
    second.join(5)
    assert not second.is_alive()
    release_first.set()
    first.join(5)

    assert not first.is_alive()
    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PlanConsumerViewMutationConflict)
    assert "active install or mutation" in str(failures[0])


def test_installer_cannot_overlap_live_mutation_transaction(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    mutation_entered = Event()
    release_mutation = Event()
    owner_failures: list[BaseException] = []

    def own_installed_mutation() -> None:
        try:
            installed = _install(layout, marker)
            with mutating_plan_consumer_view(
                layout.shot,
                installed,
                marker,
            ):
                mutation_entered.set()
                assert release_mutation.wait(5)
        except BaseException as exc:  # asserted below
            owner_failures.append(exc)

    owner = Thread(target=own_installed_mutation)
    owner.start()
    assert mutation_entered.wait(5)
    installer_failures: list[BaseException] = []

    def competing_install() -> None:
        proof = None
        try:
            _temporary, allocation = _allocate(layout, marker)
            with constructing_plan_consumer_view(
                layout,
                allocation,
                marker,
            ) as capability:
                proof = prepare_plan_consumer_view_installation(capability)
            install_plan_consumer_view(
                proof,
                layout.scratch / "plan-consumer-view",
            )
            proof = None
        except BaseException as exc:  # asserted below
            installer_failures.append(exc)
        finally:
            if proof is not None:
                discard_prepared_plan_consumer_view_installation(proof)

    competitor = Thread(target=competing_install)
    competitor.start()
    competitor.join(5)
    release_mutation.set()
    owner.join(5)

    assert not competitor.is_alive()
    assert not owner.is_alive()
    assert not owner_failures
    assert len(installer_failures) == 1
    assert isinstance(installer_failures[0], PlanConsumerViewMutationConflict)
    assert "active install or mutation" in str(installer_failures[0])


def test_mutation_cleanup_retains_preclose_neutral_until_explicit_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    original_matches = run_owner_fork_guard._descriptor_matches
    interrupted = False

    def interrupt_first_classification(
        descriptor: int,
        expected: run_owner_fork_guard.GuardedDescriptor,
    ) -> bool:
        nonlocal interrupted

        if (
            not interrupted
            and expected == run_owner_fork_registry.NEUTRALIZER_IDENTITY
            and original_matches(descriptor, expected)
        ):
            interrupted = True
            raise KeyboardInterrupt("injected before consumer neutral close")
        return original_matches(descriptor, expected)

    with (
        pytest.raises(
            PlanConsumerViewMutationConflict,
            match="retained live descriptors",
        ),
        mutating_plan_consumer_view(layout.shot, installed, marker),
    ):
        monkeypatch.setattr(
            run_owner_fork_guard,
            "_descriptor_matches",
            interrupt_first_classification,
        )

    monkeypatch.setattr(
        run_owner_fork_guard,
        "_descriptor_matches",
        original_matches,
    )
    run_owner_fork_guard.drain_inert_descriptors()
    assert interrupted
    assert not run_owner_fork_registry.INERT_DESCRIPTORS


def test_retained_mutation_cleanup_overrides_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    original_matches = run_owner_fork_guard._descriptor_matches
    interrupted = False

    def interrupt_neutral_classification(
        descriptor: int,
        expected: run_owner_fork_guard.GuardedDescriptor,
    ) -> bool:
        nonlocal interrupted
        if (
            not interrupted
            and expected == run_owner_fork_registry.NEUTRALIZER_IDENTITY
            and original_matches(descriptor, expected)
        ):
            interrupted = True
            raise KeyboardInterrupt("injected retained neutral cleanup")
        return original_matches(descriptor, expected)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="retained live descriptors",
    ) as captured, mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ):
        monkeypatch.setattr(
            run_owner_fork_guard,
            "_descriptor_matches",
            interrupt_neutral_classification,
        )
        raise ValueError("injected mutation body failure")

    monkeypatch.setattr(
        run_owner_fork_guard,
        "_descriptor_matches",
        original_matches,
    )
    assert isinstance(captured.value.__cause__, ValueError)
    assert interrupted
    run_owner_fork_guard.drain_inert_descriptors()


def test_ledger_alias_acquisition_failure_preserves_installed_registration(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    marker = _marker(layout.shot)
    installed = _install(layout, marker)
    live_ledger = layout.shot / "shot.json"
    live_ledger.write_bytes(b'{"shot":"fixture","milestones":{}}\n')
    projected_ledger = installed / "shot.json"
    os.link(live_ledger, projected_ledger)

    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match=r"registration differs|aliases the canonical shot.json inode",
    ), mutating_plan_consumer_view(layout.shot, installed, marker):
        pytest.fail("a physical shot.json alias minted mutation authority")

    projected_ledger.unlink()
    with mutating_plan_consumer_view(
        layout.shot,
        installed,
        marker,
    ) as capability:
        assert require_plan_consumer_view_mutation(capability) == installed

    assert live_ledger.read_bytes() == b'{"shot":"fixture","milestones":{}}\n'
    with pytest.raises(
        PlanConsumerViewMutationConflict,
        match="not installed",
    ), mutating_plan_consumer_view(layout.shot, installed, marker):
        pytest.fail("the successful installed claim was reusable")
