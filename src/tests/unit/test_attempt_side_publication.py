from __future__ import annotations

import asyncio
import copy
import fcntl
import gc
import hashlib
import json
import os
import signal
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import legacy_apply_replan
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.blender.tools import misc as misc_tools
from vfx_harness.blender.tools.misc import register_misc
from vfx_harness.evidence import runtime_check_publication
from vfx_harness.observability import (
    prepared_publication,
    prepared_publication_descriptors,
    prepared_publication_destinations,
    worklists,
)
from vfx_harness.orchestration import escalate, layer_replay_receipts, unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
)


def _building_guard(tmp_path: Path):
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="side-file-race",
        selection_token=token,
        reason="fixture planning",
    )
    building = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="side-file-race",
        selection_token=token,
        reason="fixture building",
    )
    selected = SimpleNamespace(selection_token=token)
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=selected,
    )
    return unit, units, plan_hash, guard


def _runtime_row(identifier: str) -> dict:
    return {
        "id": identifier,
        "layer": "1",
        "metric": "region_mean",
        "op": ">=",
        "lo": 0.5,
    }


def _private_publication_record(publication):
    """White-box access used only to assert private descriptor lifecycle."""

    return prepared_publication._registry.require_live_prepared_file(publication)


def test_prepared_side_file_commit_rehashes_the_held_read_only_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    expected_size = update.publication.temporary_identity.size
    real_pread = os.pread
    offsets: list[int] = []

    def refuse_sequential_content_io(*_args, **_kwargs):
        raise AssertionError("commit used a mutable sequential file offset")

    def tracked_pread(descriptor: int, length: int, offset: int) -> bytes:
        offsets.append(offset)
        return real_pread(descriptor, length, offset)

    monkeypatch.setattr(prepared_publication.os, "read", refuse_sequential_content_io)
    monkeypatch.setattr(prepared_publication.os, "write", refuse_sequential_content_io)
    monkeypatch.setattr(prepared_publication.os, "pread", tracked_pread)

    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
    )

    assert json.loads((tmp_path / "runtime_checks.json").read_text()) == [_runtime_row("first")]
    assert offsets == [0, expected_size]


def test_runtime_check_preparations_compare_and_swap_exact_predecessor(
    tmp_path: Path,
) -> None:
    first = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    second = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("second")],
        authority_binding="work-unit-attempt:claim-b",
    )
    assert first.publication is not None
    assert second.publication is not None

    prepared_publication.commit_prepared_file(
        first.publication,
        authority_binding="work-unit-attempt:claim-a",
    )
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="target changed after preparation",
    ):
        prepared_publication.commit_prepared_file(
            second.publication,
            authority_binding="work-unit-attempt:claim-b",
        )
    prepared_publication.discard_prepared_file(second.publication)

    assert json.loads((tmp_path / "runtime_checks.json").read_text()) == [_runtime_row("first")]


def test_side_file_preparation_cleanup_never_deletes_a_substituted_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_all = prepared_publication._descriptors.write_all

    def substitute_then_fail(descriptor: int, payload: bytes) -> None:
        original_write_all(descriptor, payload)
        temporary = next(tmp_path.glob(".runtime_checks.json.prepared.*"))
        substitute = tmp_path / "substitute"
        substitute.write_bytes(b"attacker")
        os.replace(substitute, temporary)
        raise OSError("injected failure after temp substitution")

    monkeypatch.setattr(
        prepared_publication._descriptors,
        "write_all",
        substitute_then_fail,
    )

    with pytest.raises(OSError, match="injected failure"):
        runtime_check_publication.prepare_runtime_check_update(
            tmp_path,
            [_runtime_row("first")],
            authority_binding="work-unit-attempt:claim-a",
        )

    leftovers = list(tmp_path.glob(".runtime_checks.json.prepared.*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == b"attacker"
    assert not (tmp_path / "runtime_checks.json").exists()


def test_side_file_commit_refuses_post_rename_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    attacker = tmp_path / "attacker"
    attacker.write_bytes(b"attacker")
    real_replace = prepared_publication.os.replace
    injected = False

    def substitute_after_replace(source, target, *args, **kwargs) -> None:
        nonlocal injected
        real_replace(source, target, *args, **kwargs)
        if not injected and target == "runtime_checks.json":
            injected = True
            real_replace(
                attacker.name,
                target,
                src_dir_fd=kwargs["dst_dir_fd"],
                dst_dir_fd=kwargs["dst_dir_fd"],
            )

    monkeypatch.setattr(prepared_publication.os, "replace", substitute_after_replace)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="published side-file inode changed",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )
    prepared_publication.discard_prepared_file(update.publication)

    assert (tmp_path / "runtime_checks.json").read_bytes() == b"attacker"
    assert list(tmp_path.glob(".runtime_checks.json.prepared.*")) == []


def test_side_file_commit_refuses_post_rename_same_size_rewrite_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"generation":1}\n'
    attacker = b'{"generation":2}\n'
    assert len(attacker) == len(payload)
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    expected_mtime = update.publication.temporary_identity.modified_ns
    real_replace = prepared_publication.os.replace
    injected = False

    def rewrite_after_replace(source, target, *args, **kwargs) -> None:
        nonlocal injected
        real_replace(source, target, *args, **kwargs)
        if not injected and target == "result.json":
            injected = True
            descriptor = os.open(
                target,
                os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                assert os.pwrite(descriptor, attacker, 0) == len(attacker)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            observed = os.stat(
                target,
                dir_fd=kwargs["dst_dir_fd"],
                follow_symlinks=False,
            )
            os.utime(
                target,
                ns=(observed.st_atime_ns, expected_mtime),
                dir_fd=kwargs["dst_dir_fd"],
                follow_symlinks=False,
            )

    monkeypatch.setattr(prepared_publication.os, "replace", rewrite_after_replace)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="bytes do not match the prepared payload",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )
    prepared_publication.discard_prepared_file(update.publication)

    assert injected is True
    assert (tmp_path / "result.json").read_bytes() == attacker
    assert list(tmp_path.glob(".result.json.prepared.*")) == []


def test_side_file_commit_refuses_lock_contention_without_waiting(
    tmp_path: Path,
) -> None:
    first = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    contender = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("contender")],
        authority_binding="work-unit-attempt:claim-b",
    )
    assert first.publication is not None
    assert contender.publication is not None
    external_lock = os.open(
        first.publication.lock_parent / first.publication.lock_name,
        os.O_RDWR,
    )
    fcntl.flock(external_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="already committing",
        ):
            prepared_publication.commit_prepared_file(
                contender.publication,
                authority_binding="work-unit-attempt:claim-b",
            )
    finally:
        fcntl.flock(external_lock, fcntl.LOCK_UN)
        os.close(external_lock)
        prepared_publication.discard_prepared_file(first.publication)
        prepared_publication.discard_prepared_file(contender.publication)
    assert not (tmp_path / "runtime_checks.json").exists()


def test_question_preparations_cannot_overwrite_a_concurrent_append(
    tmp_path: Path,
) -> None:
    first = escalate.prepare_question(
        tmp_path,
        layer="1",
        question="First decision?",
        assumption="first assumption",
        affected_layers=["1"],
        authority_binding="work-unit-attempt:claim-a",
    )
    second = escalate.prepare_question(
        tmp_path,
        layer="2",
        question="Second decision?",
        assumption="second assumption",
        affected_layers=["2"],
        authority_binding="work-unit-attempt:claim-b",
    )
    assert first.update.publication is not None
    assert second.update.publication is not None

    escalate.commit_prepared_question(
        first,
        authority_binding="work-unit-attempt:claim-a",
    )
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="target changed after preparation",
    ):
        escalate.commit_prepared_question(
            second,
            authority_binding="work-unit-attempt:claim-b",
        )
    escalate.discard_prepared_question(second)

    questions = escalate.load(tmp_path)
    assert [row["question"] for row in questions] == ["First decision?"]


def test_duplicate_question_stages_exact_existing_bytes_for_cas(
    tmp_path: Path,
) -> None:
    created = escalate.prepare_question(
        tmp_path,
        layer="1",
        question="Which framing wins?",
        assumption="use the brief",
        global_decision=True,
        authority_binding="work-unit-attempt:claim-a",
    )
    escalate.commit_prepared_question(
        created,
        authority_binding="work-unit-attempt:claim-a",
    )
    before = (tmp_path / escalate.QUESTIONS).read_bytes()

    duplicate = escalate.prepare_question(
        tmp_path,
        layer="2",
        question="  WHICH FRAMING WINS?  ",
        assumption="use the reference",
        global_decision=True,
        authority_binding="work-unit-attempt:claim-b",
    )
    assert duplicate.update.publication is not None
    assert duplicate.update.result == (1, False)

    escalate.commit_prepared_question(
        duplicate,
        authority_binding="work-unit-attempt:claim-b",
    )
    assert (tmp_path / escalate.QUESTIONS).read_bytes() == before


def test_duplicate_question_refuses_a_changed_predecessor(
    tmp_path: Path,
) -> None:
    original = escalate.prepare_question(
        tmp_path,
        layer="1",
        question="Which framing wins?",
        assumption="use the brief",
        global_decision=True,
        authority_binding="work-unit-attempt:claim-a",
    )
    escalate.commit_prepared_question(
        original,
        authority_binding="work-unit-attempt:claim-a",
    )
    duplicate = escalate.prepare_question(
        tmp_path,
        layer="1",
        question="which framing wins?",
        assumption="use the brief",
        global_decision=True,
        authority_binding="work-unit-attempt:claim-b",
    )
    concurrent = escalate.prepare_question(
        tmp_path,
        layer="2",
        question="Which timing wins?",
        assumption="use the animatic",
        affected_layers=["2"],
        authority_binding="work-unit-attempt:claim-c",
    )
    escalate.commit_prepared_question(
        concurrent,
        authority_binding="work-unit-attempt:claim-c",
    )

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="target changed after preparation",
    ):
        escalate.commit_prepared_question(
            duplicate,
            authority_binding="work-unit-attempt:claim-b",
        )
    escalate.discard_prepared_question(duplicate)

    assert [row["question"] for row in escalate.load(tmp_path)] == [
        "Which framing wins?",
        "Which timing wins?",
    ]


def test_builder_question_refuses_forged_noop_before_reporting_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers = {}

    def capture_tool(name, _description, _schema):
        def decorate(handler):
            handlers[name] = handler
            return SimpleNamespace(handler=handler)

        return decorate

    forged = escalate.PreparedQuestion(
        update=prepared_publication.PreparedFileUpdate(
            publication=None,
            result=(1, False),
        ),
        question="Which framing wins?",
        assumption="Use the brief",
    )
    reported = False

    def forged_preparation(*_args, **_kwargs):
        return forged

    def report_question(_prepared):
        nonlocal reported
        reported = True
        return 1

    monkeypatch.setattr(misc_tools, "tool", capture_tool)
    monkeypatch.setattr(escalate, "prepare_question", forged_preparation)
    monkeypatch.setattr(escalate, "log_committed_question", report_question)

    async def no_blender_call(*_args, **_kwargs):
        raise AssertionError("supervisor question publication must not call Blender")

    register_misc(
        session=object(),
        _call=no_blender_call,
        comparison_state={},
        comparison_locks={},
        shot_dir=tmp_path,
        layer_id="1",
        assets_dir=None,
        feedback_policy={},
        mutation_roles=(),
        scope_baseline=set(),
        unit_scope={},
        _black_frame_note=lambda _row: "",
        _black_search_stop=lambda: "",
        _register_candidate=lambda _row: None,
    )

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="lacks an authoritative CAS publication",
    ):
        asyncio.run(
            handlers["ask_supervisor"](
                {
                    "question": "Which framing wins?",
                    "assumption": "Use the brief",
                    "affected_layers": ["1"],
                }
            )
        )

    assert reported is False
    assert not (tmp_path / escalate.QUESTIONS).exists()


def test_prepared_question_without_cas_authority_cannot_report_success(
    tmp_path: Path,
) -> None:
    forged = escalate.PreparedQuestion(
        update=prepared_publication.PreparedFileUpdate(
            publication=None,
            result=(1, False),
        ),
        question="Which framing wins?",
        assumption="use the brief",
    )

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="lacks an authoritative CAS publication",
    ):
        escalate.commit_prepared_question(
            forged,
            authority_binding="work-unit-attempt:claim-a",
        )

    assert not (tmp_path / escalate.QUESTIONS).exists()


def test_wrong_attempt_binding_cannot_publish_prepared_side_file(
    tmp_path: Path,
) -> None:
    update = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("bound")],
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="another authority binding",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-b",
        )
    prepared_publication.discard_prepared_file(update.publication)
    assert not (tmp_path / "runtime_checks.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination", Path("/forged/result.json")),
        ("destination_name", "forged.json"),
        ("predecessor", prepared_publication.FileIdentity(1, 2, 3, 4, 5)),
        ("authority_binding", "work-unit-attempt:forged"),
        ("lock_name", "forged.lock"),
    ],
)
def test_prepared_side_file_authority_cannot_be_replaced(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    try:
        with pytest.raises(TypeError, match="dataclass"):
            replace(update.publication, **{field: value})
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="cannot be copied",
        ):
            copy.copy(update.publication)
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="cannot be copied",
        ):
            copy.deepcopy(update.publication)
    finally:
        prepared_publication.discard_prepared_file(update.publication)


def test_unregistered_prepared_side_file_cannot_close_live_resources(
    tmp_path: Path,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    live_descriptor = _private_publication_record(update.publication).temporary_descriptor
    forged = object.__new__(prepared_publication.PreparedFilePublication)
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="unregistered",
    ):
        prepared_publication.discard_prepared_file(forged)
    assert os.fstat(live_descriptor).st_size > 0
    prepared_publication.discard_prepared_file(update.publication)


def test_side_file_commit_refuses_replaced_shot_root(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot"
    displaced = tmp_path / "displaced-shot"
    shot.mkdir()
    update = prepared_publication.prepare_file_update(
        shot,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None

    os.replace(shot, displaced)
    shot.mkdir()
    os.replace(displaced / "state", shot / "state")
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="shot root changed after side-file preparation",
        ):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )
    finally:
        prepared_publication.discard_prepared_file(update.publication)

    assert not (shot / "result.json").exists()
    assert not (displaced / "result.json").exists()


def test_prepared_payload_verification_refuses_wrong_expected_digest(
    tmp_path: Path,
) -> None:
    expected = b'{"authority":"expected"}\n'
    unexpected = b'{"authority":"forged"}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (unexpected, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="do not match typed authority",
        ):
            prepared_publication.verify_prepared_file_payload(
                update.publication,
                expected_sha256=hashlib.sha256(expected).hexdigest(),
            )
    finally:
        prepared_publication.discard_prepared_file(update.publication)

    assert not (tmp_path / "result.json").exists()


def test_staged_descriptor_is_read_only_through_verification_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    verification = prepared_publication.verify_prepared_file_payload(
        update.publication,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    temporary_descriptor = _private_publication_record(update.publication).temporary_descriptor
    real_replace = prepared_publication.os.replace
    attempted_write = False

    def inject_write_before_replace(*args, **kwargs) -> None:
        nonlocal attempted_write
        attempted_write = True
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.pwrite(temporary_descriptor, b"2", 14)
        real_replace(*args, **kwargs)

    monkeypatch.setattr(prepared_publication.os, "replace", inject_write_before_replace)
    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
        payload_verification=verification,
    )

    assert attempted_write is True
    assert (tmp_path / "result.json").read_bytes() == payload


def test_staged_full_identity_detects_ctime_change_with_restored_mtime(
    tmp_path: Path,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    temporary = update.publication.temporary
    expected_identity = update.publication.temporary_identity
    before = temporary.stat()
    temporary.write_bytes(b'{"generation":2}\n')
    os.utime(
        temporary,
        ns=(before.st_atime_ns, expected_identity.modified_ns),
    )
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="prepared side-file inode changed",
        ):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )
    finally:
        prepared_publication.discard_prepared_file(update.publication)


def test_registration_return_boundary_cleans_unreachable_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    actual_register = registry.register_prepared_file

    def interrupt_after_registration(*args, **kwargs):
        actual_register(*args, **kwargs)
        raise KeyboardInterrupt("injected after publication registry insertion")

    monkeypatch.setattr(registry, "register_prepared_file", interrupt_after_registration)
    with pytest.raises(KeyboardInterrupt, match="registry insertion"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )
    gc.collect()
    assert list(tmp_path.glob(".result.json.prepared.*")) == []
    assert not (tmp_path / "result.json").exists()


def test_acquisition_arm_return_boundary_retires_pending_registry_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    baseline = set(registry._PENDING_ACQUISITIONS)
    actual_arm = registry.arm_descriptor_acquisition

    def interrupt_after_arm(token: object) -> None:
        actual_arm(token)
        raise KeyboardInterrupt("injected after acquisition registry insertion")

    monkeypatch.setattr(registry, "arm_descriptor_acquisition", interrupt_after_arm)
    with pytest.raises(KeyboardInterrupt, match="acquisition registry insertion"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert set(registry._PENDING_ACQUISITIONS) == baseline
    assert not (tmp_path / "result.json").exists()


def test_directory_open_return_boundary_retires_registered_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_open = prepared_publication._open_pending_real_directory
    opened_descriptor: int | None = None

    def interrupt_after_open(*args, **kwargs) -> int:
        nonlocal opened_descriptor
        opened_descriptor = actual_open(*args, **kwargs)
        raise KeyboardInterrupt("injected after tracked directory open")

    monkeypatch.setattr(
        prepared_publication,
        "_open_pending_real_directory",
        interrupt_after_open,
    )
    with pytest.raises(KeyboardInterrupt, match="tracked directory open"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert opened_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(opened_descriptor)


def test_temporary_open_return_boundary_closes_and_unlinks_registered_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    actual_open = registry.open_pending_descriptor
    opened_descriptor: int | None = None
    interrupted = False

    def interrupt_after_temporary_open(token, path, flags, mode=0o777, *, dir_fd=None):
        nonlocal interrupted, opened_descriptor
        descriptor = actual_open(token, path, flags, mode, dir_fd=dir_fd)
        if str(path).startswith(".result.json.prepared.") and not interrupted:
            interrupted = True
            opened_descriptor = descriptor
            raise KeyboardInterrupt("injected after tracked temporary open")
        return descriptor

    monkeypatch.setattr(
        registry,
        "open_pending_descriptor",
        interrupt_after_temporary_open,
    )
    with pytest.raises(KeyboardInterrupt, match="tracked temporary open"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert opened_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(opened_descriptor)
    assert list(tmp_path.glob(".result.json.prepared.*")) == []


def test_preexisting_temp_collision_is_never_claimed_by_interrupted_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b"seed", None),
        authority_binding="work-unit-attempt:seed",
    )
    assert seed.publication is not None
    lock = seed.publication.lock_parent / seed.publication.lock_name
    prepared_publication.discard_prepared_file(seed.publication)
    collision = tmp_path / ".result.json.prepared.fixed"
    os.link(lock, collision)

    def interrupt_before_collision_clear(_token: object) -> None:
        raise KeyboardInterrupt("injected before collision candidate clear")

    monkeypatch.setattr(prepared_publication.secrets, "token_hex", lambda _size: "fixed")
    monkeypatch.setattr(
        prepared_publication._registry,
        "clear_pending_temporary",
        interrupt_before_collision_clear,
    )
    with pytest.raises(KeyboardInterrupt, match="collision candidate clear"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b"payload", None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert collision.exists()
    assert collision.stat().st_ino == lock.stat().st_ino
    assert prepared_publication._registry._PENDING_ACQUISITIONS == {}
    collision.unlink()


def test_created_temp_capture_interruption_keeps_exact_cleanup_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    actual_capture = registry.GuardedDescriptor.capture
    interrupted_descriptor: int | None = None

    def interrupt_created_capture(cls, descriptor: int):
        nonlocal interrupted_descriptor
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if ".result.json.prepared." in target and interrupted_descriptor is None:
            interrupted_descriptor = descriptor
            raise KeyboardInterrupt("injected staged identity capture interruption")
        return actual_capture(descriptor)

    monkeypatch.setattr(
        registry.GuardedDescriptor,
        "capture",
        classmethod(interrupt_created_capture),
    )
    with pytest.raises(KeyboardInterrupt, match="staged identity capture"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b"payload", None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert interrupted_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(interrupted_descriptor)
    assert list(tmp_path.glob(".result.json.prepared.*")) == []
    assert registry._PENDING_ACQUISITIONS == {}


def test_writable_to_readonly_handoff_retains_an_exact_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    actual_retire = registry.retire_pending_descriptor
    writable_descriptor: int | None = None
    readonly_was_registered = False

    def interrupt_after_writable_retirement(token: object, descriptor: int) -> None:
        nonlocal readonly_was_registered, writable_descriptor
        access = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
        if access != os.O_RDWR or not list(tmp_path.glob(".result.json.prepared.*")):
            actual_retire(token, descriptor)
            return
        writer = os.fstat(descriptor)
        acquisition = registry._PENDING_ACQUISITIONS[token]
        readonly_was_registered = any(
            guarded.descriptor != descriptor
            and (fcntl.fcntl(guarded.descriptor, fcntl.F_GETFL) & os.O_ACCMODE) == os.O_RDONLY
            and (os.fstat(guarded.descriptor).st_dev, os.fstat(guarded.descriptor).st_ino)
            == (writer.st_dev, writer.st_ino)
            for guarded in acquisition.descriptors
        )
        writable_descriptor = descriptor
        actual_retire(token, descriptor)
        raise KeyboardInterrupt("injected after writable staged descriptor retirement")

    monkeypatch.setattr(
        registry,
        "retire_pending_descriptor",
        interrupt_after_writable_retirement,
    )
    with pytest.raises(KeyboardInterrupt, match="writable staged descriptor"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert readonly_was_registered
    assert writable_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(writable_descriptor)
    assert registry._PENDING_ACQUISITIONS == {}
    assert list(tmp_path.glob(".result.json.prepared.*")) == []


def test_register_handoff_interruption_restores_pending_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    baseline_publications = set(registry._PUBLICATIONS)
    interrupted = False

    class InterruptingPendingAcquisitions(dict):
        def pop(self, key, *args):
            nonlocal interrupted
            result = super().pop(key, *args)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt("injected during publication handoff")
            return result

    monkeypatch.setattr(
        registry,
        "_PENDING_ACQUISITIONS",
        InterruptingPendingAcquisitions(registry._PENDING_ACQUISITIONS),
    )
    with pytest.raises(KeyboardInterrupt, match="publication handoff"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert set(registry._PUBLICATIONS) == baseline_publications
    assert registry._PENDING_ACQUISITIONS == {}
    assert list(tmp_path.glob(".result.json.prepared.*")) == []


def test_lock_acquisition_return_boundary_releases_lock_and_preserves_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    actual_flock = fcntl.flock
    interrupted = False

    def interrupt_after_lock(descriptor: int, operation: int) -> None:
        nonlocal interrupted
        actual_flock(descriptor, operation)
        if operation & fcntl.LOCK_EX and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after publication lock acquisition")

    with monkeypatch.context() as patch:
        patch.setattr(prepared_publication.fcntl, "flock", interrupt_after_lock)
        with pytest.raises(KeyboardInterrupt, match="publication lock acquisition"):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )

    contender = os.open(
        update.publication.lock_parent / update.publication.lock_name,
        os.O_RDWR,
    )
    try:
        actual_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual_flock(contender, fcntl.LOCK_UN)
    finally:
        os.close(contender)

    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
    )
    assert (tmp_path / "result.json").exists()


def test_payload_verification_is_one_generation_exact_object_and_binding(
    tmp_path: Path,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    binding = object()
    verification = prepared_publication.verify_prepared_file_payload(
        update.publication,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        transaction_binding=binding,
    )
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="cannot be copied",
    ):
        copy.copy(verification)
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="cannot be copied",
    ):
        copy.deepcopy(verification)
    with pytest.raises(TypeError, match="dataclass"):
        replace(verification)
    forged = object.__new__(prepared_publication.PreparedFilePayloadVerification)
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="unregistered",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
            payload_verification=forged,
            transaction_binding=binding,
        )
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="exact live publication generation",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
            payload_verification=verification,
            transaction_binding=object(),
        )
    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
        payload_verification=verification,
        transaction_binding=binding,
    )
    assert (tmp_path / "result.json").read_bytes() == payload


def test_minted_payload_verification_cannot_be_bypassed_or_dropped(
    tmp_path: Path,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    binding = object()
    verification = prepared_publication.verify_prepared_file_payload(
        update.publication,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        transaction_binding=binding,
    )

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="exact latest payload verification",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )
    del verification
    gc.collect()
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="exact latest payload verification",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )

    replacement = prepared_publication.verify_prepared_file_payload(
        update.publication,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        transaction_binding=binding,
    )
    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
        payload_verification=replacement,
        transaction_binding=binding,
    )


def test_dead_payload_proof_row_cannot_collide_with_another_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_a = b'{"generation":"a"}\n'
    payload_b = b'{"generation":"b"}\n'
    update_a = prepared_publication.prepare_file_update(
        tmp_path,
        "a.json",
        lambda _current: (payload_a, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    update_b = prepared_publication.prepare_file_update(
        tmp_path,
        "b.json",
        lambda _current: (payload_b, None),
        authority_binding="work-unit-attempt:claim-b",
    )
    assert update_a.publication is not None
    assert update_b.publication is not None
    proof_a = prepared_publication.verify_prepared_file_payload(
        update_a.publication,
        expected_sha256=hashlib.sha256(payload_a).hexdigest(),
    )
    stale_id = id(proof_a)
    monkeypatch.setattr(
        prepared_publication._registry,
        "_verification_gone",
        lambda *_args: None,
    )
    del proof_a
    gc.collect()
    assert prepared_publication._registry._VERIFICATIONS[stale_id].reference() is None

    proof_b = prepared_publication.verify_prepared_file_payload(
        update_b.publication,
        expected_sha256=hashlib.sha256(payload_b).hexdigest(),
    )
    if stale_id in prepared_publication._registry._VERIFICATIONS:
        assert prepared_publication._registry._VERIFICATIONS[stale_id].reference() is proof_b
    prepared_publication.commit_prepared_file(
        update_b.publication,
        authority_binding="work-unit-attempt:claim-b",
        payload_verification=proof_b,
    )
    prepared_publication.discard_prepared_file(update_a.publication)
    assert (tmp_path / "b.json").read_bytes() == payload_b


def test_stored_commit_policy_cannot_reenter_commit_or_discard(
    tmp_path: Path,
) -> None:
    payload = b'{"generation":1}\n'
    authorization = object()
    transaction_binding = object()

    def attempt_reentry(
        observed_authorization: object,
        observed_transaction_binding: object,
    ) -> None:
        assert observed_authorization is authorization
        assert observed_transaction_binding is transaction_binding
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="already committing",
        ):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
                commit_authorization=authorization,
                transaction_binding=transaction_binding,
            )
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="already committing",
        ):
            prepared_publication.discard_prepared_file(update.publication)

    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
        commit_policy=attempt_reentry,
    )
    assert update.publication is not None

    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
        commit_authorization=authorization,
        transaction_binding=transaction_binding,
    )
    assert (tmp_path / "result.json").read_bytes() == payload


def test_prepared_publication_does_not_expose_lock_authority_descriptor(
    tmp_path: Path,
) -> None:
    authorization = object()
    transaction_binding = object()

    def prove_lock_still_held(
        observed_authorization: object,
        observed_transaction_binding: object,
    ) -> None:
        assert observed_authorization is authorization
        assert observed_transaction_binding is transaction_binding
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
        commit_policy=prove_lock_still_held,
    )
    assert update.publication is not None
    with pytest.raises(AttributeError):
        _lock_descriptor = update.publication.lock_descriptor
    with pytest.raises(AttributeError):
        _temporary_descriptor = update.publication.temporary_descriptor
    with pytest.raises(AttributeError):
        _parent_descriptor = update.publication.destination_parent_descriptor
    competing_lock = os.open(
        update.publication.lock_parent / update.publication.lock_name,
        os.O_RDWR,
    )

    try:
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
            commit_authorization=authorization,
            transaction_binding=transaction_binding,
        )
    finally:
        os.close(competing_lock)
        prepared_publication.discard_prepared_file(update.publication)

    assert (tmp_path / "result.json").exists()


def test_stored_commit_policy_cannot_substitute_same_size_payload(
    tmp_path: Path,
) -> None:
    payload = b'{"generation":1}\n'
    replacement = b'{"generation":2}\n'
    authorization = object()
    transaction_binding = object()

    def substitute_payload(
        observed_authorization: object,
        observed_transaction_binding: object,
    ) -> None:
        assert observed_authorization is authorization
        assert observed_transaction_binding is transaction_binding
        temporary.write_bytes(replacement)
        os.utime(
            temporary,
            ns=(before.st_atime_ns, expected_identity.modified_ns),
        )

    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
        commit_policy=substitute_payload,
    )
    assert update.publication is not None
    temporary = update.publication.temporary
    expected_identity = update.publication.temporary_identity
    before = temporary.stat()
    verification = prepared_publication.verify_prepared_file_payload(
        update.publication,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        transaction_binding=transaction_binding,
    )

    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="prepared side-file inode changed",
        ):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
                payload_verification=verification,
                commit_authorization=authorization,
                transaction_binding=transaction_binding,
            )
    finally:
        prepared_publication.discard_prepared_file(update.publication)

    assert not (tmp_path / "result.json").exists()


def test_policy_bound_publication_refuses_missing_commit_authorization(
    tmp_path: Path,
) -> None:
    authorization = object()
    transaction_binding = object()
    observed: list[object] = []

    def require_authorization(
        candidate: object,
        observed_transaction_binding: object,
    ) -> None:
        observed.append(candidate)
        assert candidate is authorization
        assert observed_transaction_binding is transaction_binding

    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
        commit_policy=require_authorization,
    )
    assert update.publication is not None

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="requires its commit authorization",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )

    assert observed == []
    assert not (tmp_path / "result.json").exists()
    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
        commit_authorization=authorization,
        transaction_binding=transaction_binding,
    )
    assert observed == [authorization]
    assert (tmp_path / "result.json").exists()


def test_unbound_publication_refuses_extraneous_commit_authorization(
    tmp_path: Path,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="cannot consume a commit authorization",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
            commit_authorization=object(),
        )

    assert not (tmp_path / "result.json").exists()
    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
    )
    assert (tmp_path / "result.json").exists()


def test_commit_caller_cannot_substitute_a_different_stored_policy(
    tmp_path: Path,
) -> None:
    authorization = object()
    transaction_binding = object()
    stored_calls: list[object] = []
    substitute_calls: list[object] = []

    def stored_policy(candidate: object, observed_transaction_binding: object) -> None:
        assert observed_transaction_binding is transaction_binding
        stored_calls.append(candidate)

    def substitute_policy(candidate: object, _transaction_binding: object) -> None:
        substitute_calls.append(candidate)

    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
        commit_policy=stored_policy,
    )
    assert update.publication is not None

    with pytest.raises(TypeError, match="unexpected keyword argument 'commit_policy'"):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
            commit_authorization=authorization,
            transaction_binding=transaction_binding,
            commit_policy=substitute_policy,
        )

    assert stored_calls == []
    assert substitute_calls == []
    assert not (tmp_path / "result.json").exists()
    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
        commit_authorization=authorization,
        transaction_binding=transaction_binding,
    )
    assert stored_calls == [authorization]
    assert substitute_calls == []
    assert (tmp_path / "result.json").exists()


def test_post_rename_cleanup_failure_never_restores_commit_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    temporary_descriptor = _private_publication_record(update.publication).temporary_descriptor
    registry = prepared_publication._registry
    actual_neutralize = registry._neutralize_guarded_descriptor
    failed = False

    def fail_first_neutralization(guarded, **kwargs) -> bool:
        nonlocal failed
        if guarded.descriptor == temporary_descriptor and not failed:
            failed = True
            raise OSError("injected descriptor cleanup failure")
        return actual_neutralize(guarded, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            registry,
            "_neutralize_guarded_descriptor",
            fail_first_neutralization,
        )
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="transaction cleanup failed",
        ):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )

    assert (tmp_path / "result.json").read_bytes() == payload
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="expired",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )

    prepared_publication.discard_prepared_file(update.publication)
    with pytest.raises(OSError):
        os.fstat(temporary_descriptor)


def test_body_and_cleanup_failures_preserve_both_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = object()
    transaction_binding = object()

    def refuse_policy(
        _authorization: object,
        _transaction_binding: object,
    ) -> None:
        raise ValueError("policy refusal")

    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
        commit_policy=refuse_policy,
    )
    assert update.publication is not None
    registry = prepared_publication._registry
    real_cancel = registry.cancel_prepared_file_commit

    def cancel_then_report_failure(*args, **kwargs) -> None:
        real_cancel(*args, **kwargs)
        raise OSError("cleanup blew")

    monkeypatch.setattr(
        registry,
        "cancel_prepared_file_commit",
        cancel_then_report_failure,
    )

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="transaction cleanup failed",
    ) as observed:
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
            transaction_binding=transaction_binding,
            commit_authorization=authorization,
        )

    assert isinstance(observed.value.__cause__, ValueError)
    assert str(observed.value.__cause__) == "policy refusal"
    assert any(
        "OSError: cleanup blew" in note
        for note in getattr(observed.value, "__notes__", ())
    )
    prepared_publication.discard_prepared_file(update.publication)


def test_interrupted_commit_completion_is_discard_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    temporary_descriptor = _private_publication_record(update.publication).temporary_descriptor

    def interrupt_before_completion(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt("injected before commit completion")

    with monkeypatch.context() as patch:
        patch.setattr(
            prepared_publication._registry,
            "complete_prepared_file_commit",
            interrupt_before_completion,
        )
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="transaction cleanup failed",
        ):
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )

    assert (tmp_path / "result.json").read_bytes() == payload
    prepared_publication.discard_prepared_file(update.publication)
    with pytest.raises(OSError):
        os.fstat(temporary_descriptor)


def test_dup2_return_interruption_closes_neutralized_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    temporary_descriptor = _private_publication_record(update.publication).temporary_descriptor
    actual_dup2 = prepared_publication_descriptors.os.dup2
    interrupted = False

    def interrupt_after_dup2(*args, **kwargs) -> int:
        nonlocal interrupted
        result = actual_dup2(*args, **kwargs)
        if args[1] == temporary_descriptor and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after descriptor neutralization")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            prepared_publication_descriptors.os,
            "dup2",
            interrupt_after_dup2,
        )
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="cleanup reported",
        ):
            prepared_publication.discard_prepared_file(update.publication)

    with pytest.raises(OSError):
        os.fstat(temporary_descriptor)
    prepared_publication.discard_prepared_file(update.publication)


def test_dup2_return_interruption_still_closes_neutral_descriptor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    final_descriptor = _private_publication_record(update.publication).shot_descriptor
    actual_dup2 = prepared_publication_descriptors.os.dup2
    interrupted = False

    def interrupt_after_final_dup2(*args, **kwargs) -> int:
        nonlocal interrupted
        result = actual_dup2(*args, **kwargs)
        if args[1] == final_descriptor and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after final descriptor neutralization")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            prepared_publication_descriptors.os,
            "dup2",
            interrupt_after_final_dup2,
        )
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="cleanup reported",
        ):
            prepared_publication.discard_prepared_file(update.publication)

    with pytest.raises(OSError):
        os.fstat(final_descriptor)
    prepared_publication.discard_prepared_file(update.publication)


def test_signal_mask_is_restored_when_blocking_call_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_pthread_sigmask = prepared_publication_descriptors.signal.pthread_sigmask
    initial = actual_pthread_sigmask(signal.SIG_BLOCK, set())
    calls = 0

    def interrupt_after_mask_change(how, mask):
        nonlocal calls
        calls += 1
        result = actual_pthread_sigmask(how, mask)
        if calls == 2:
            raise KeyboardInterrupt("injected after signal-mask change")
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                prepared_publication_descriptors.signal,
                "pthread_sigmask",
                interrupt_after_mask_change,
            )
            with (
                pytest.raises(KeyboardInterrupt, match="signal-mask change"),
                prepared_publication_descriptors.block_deferred_signals(),
            ):
                raise AssertionError("mask interruption must precede the body")
        assert actual_pthread_sigmask(signal.SIG_BLOCK, set()) == initial
    finally:
        actual_pthread_sigmask(signal.SIG_SETMASK, initial)


def test_pending_cleanup_transient_failure_leaves_no_unowned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    actual_neutralize = registry._neutralize_guarded_descriptor
    failed_descriptor: int | None = None
    failed = False
    write_failed = False

    def fail_once(guarded, **kwargs) -> bool:
        nonlocal failed, failed_descriptor
        if write_failed and not failed:
            failed = True
            failed_descriptor = guarded.descriptor
            raise KeyboardInterrupt("injected pending cleanup interruption")
        return actual_neutralize(guarded, **kwargs)

    def fail_write(_descriptor: int, _payload: bytes) -> None:
        nonlocal write_failed
        write_failed = True
        raise OSError("injected preparation failure")

    monkeypatch.setattr(registry, "_neutralize_guarded_descriptor", fail_once)
    monkeypatch.setattr(prepared_publication._descriptors, "write_all", fail_write)
    with pytest.raises(OSError, match="injected preparation failure"):
        prepared_publication.prepare_file_update(
            tmp_path,
            "result.json",
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert failed_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(failed_descriptor)
    assert registry._PENDING_ACQUISITIONS == {}


def test_prepared_side_file_is_thread_bound_and_owner_can_discard(
    tmp_path: Path,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    failures: list[BaseException] = []

    def commit_from_foreign_thread() -> None:
        try:
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )
        except BaseException as exc:
            failures.append(exc)

    worker = Thread(target=commit_from_foreign_thread)
    worker.start()
    worker.join(5)
    assert len(failures) == 1
    assert isinstance(failures[0], prepared_publication.FilePublicationConflict)
    assert "another process or thread" in str(failures[0])
    prepared_publication.discard_prepared_file(update.publication)


def test_prepared_side_file_is_inert_in_a_fork_child(tmp_path: Path) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    temporary_descriptor = _private_publication_record(update.publication).temporary_descriptor
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertion is transmitted to parent
        os.close(read_fd)
        descriptor_closed = False
        try:
            os.fstat(temporary_descriptor)
        except OSError:
            descriptor_closed = True
        try:
            prepared_publication.commit_prepared_file(
                update.publication,
                authority_binding="work-unit-attempt:claim-a",
            )
        except prepared_publication.FilePublicationConflict:
            os.write(
                write_fd,
                b"refused-closed" if descriptor_closed else b"refused-open",
            )
        else:
            os.write(write_fd, b"committed")
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    observed = os.read(read_fd, 64)
    os.close(read_fd)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert observed == b"refused-closed"
    prepared_publication.discard_prepared_file(update.publication)


def test_prepared_side_file_discard_and_commit_expire_exact_capability(
    tmp_path: Path,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    prepared_publication.discard_prepared_file(update.publication)
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="expired",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )
    prepared_publication.discard_prepared_file(update.publication)


def test_registered_discard_never_unlinks_a_substituted_lock_inode(
    tmp_path: Path,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b"payload", None),
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    temporary = update.publication.temporary
    lock = update.publication.lock_parent / update.publication.lock_name
    staged_descriptor = _private_publication_record(update.publication).temporary_descriptor
    temporary.unlink()
    os.link(lock, temporary)

    prepared_publication.discard_prepared_file(update.publication)

    assert temporary.exists()
    assert temporary.stat().st_ino == lock.stat().st_ino
    with pytest.raises(OSError):
        os.fstat(staged_descriptor)
    temporary.unlink()


def test_prepared_side_file_registry_lifetime_cleans_abandoned_capability(
    tmp_path: Path,
) -> None:
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    publication = update.publication
    assert publication is not None
    temporary = publication.temporary
    descriptor = _private_publication_record(publication).temporary_descriptor
    del publication
    del update
    gc.collect()
    assert not temporary.exists()
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_abandoned_capability_retries_transient_descriptor_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = prepared_publication._registry
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (b'{"generation":1}\n', None),
        authority_binding="work-unit-attempt:claim-a",
    )
    publication = update.publication
    assert publication is not None
    publication_id = id(publication)
    descriptor = _private_publication_record(publication).temporary_descriptor
    actual_neutralize = registry._neutralize_guarded_descriptor
    failed = False

    def fail_once(guarded, **kwargs) -> bool:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected abandoned cleanup failure")
        return actual_neutralize(guarded, **kwargs)

    monkeypatch.setattr(registry, "_neutralize_guarded_descriptor", fail_once)
    del publication
    del update
    gc.collect()

    assert publication_id not in registry._PUBLICATIONS
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert list(tmp_path.glob(".result.json.prepared.*")) == []


def test_consumed_capability_gc_never_closes_reused_same_inode_descriptor(
    tmp_path: Path,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    publication = update.publication
    assert publication is not None
    old_temporary_descriptor = _private_publication_record(publication).temporary_descriptor
    prepared_publication.commit_prepared_file(
        publication,
        authority_binding="work-unit-attempt:claim-a",
    )
    with pytest.raises(AttributeError):
        _stale_descriptor = publication.temporary_descriptor

    reopened: list[int] = []
    while not reopened or reopened[-1] < old_temporary_descriptor:
        reopened.append(os.open(tmp_path / "result.json", os.O_RDONLY))
    assert reopened[-1] == old_temporary_descriptor
    reused = reopened[-1]
    try:
        del publication
        del update
        gc.collect()
        assert os.fstat(reused).st_size == len(payload)
    finally:
        for descriptor in reversed(reopened):
            os.close(descriptor)


def test_cleanup_state_advances_before_same_inode_descriptor_slot_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"generation":1}\n'
    update = prepared_publication.prepare_file_update(
        tmp_path,
        "result.json",
        lambda _current: (payload, None),
        authority_binding="work-unit-attempt:claim-a",
    )
    publication = update.publication
    assert publication is not None
    old_descriptor = _private_publication_record(publication).temporary_descriptor
    registry = prepared_publication._registry
    actual_neutralize = registry._neutralize_guarded_descriptor
    interrupted = False

    def interrupt_after_retirement(guarded, **kwargs) -> bool:
        nonlocal interrupted
        retired = actual_neutralize(guarded, **kwargs)
        if guarded.descriptor == old_descriptor and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after exact descriptor retirement")
        return retired

    with monkeypatch.context() as patch:
        patch.setattr(
            registry,
            "_neutralize_guarded_descriptor",
            interrupt_after_retirement,
        )
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="transaction cleanup failed",
        ):
            prepared_publication.commit_prepared_file(
                publication,
                authority_binding="work-unit-attempt:claim-a",
            )

    reopened: list[int] = []
    while not reopened or reopened[-1] < old_descriptor:
        reopened.append(os.open(tmp_path / "result.json", os.O_RDONLY))
    assert reopened[-1] == old_descriptor
    foreign = reopened[-1]
    try:
        prepared_publication.discard_prepared_file(publication)
        assert os.fstat(foreign).st_size == len(payload)
    finally:
        for descriptor in reversed(reopened):
            os.close(descriptor)


def test_worklist_preparation_does_not_block_replan_and_stale_commit_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit, units, plan_hash, guard = _building_guard(tmp_path)
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    failures: list[BaseException] = []
    original_write_all = prepared_publication._descriptors.write_all

    def blocked_write(descriptor: int, payload: bytes) -> None:
        prepare_started.set()
        assert release_prepare.wait(5)
        original_write_all(descriptor, payload)

    monkeypatch.setattr(
        prepared_publication._descriptors,
        "write_all",
        blocked_write,
    )

    async def no_blender_call(*_args, **_kwargs):
        raise AssertionError("worklist publication must not call Blender")

    _script_map, _find, worklist, *_rest = register_misc(
        session=object(),
        _call=no_blender_call,
        comparison_state={
            "unit_id": unit.id,
            "unit_hash": unit_state.unit_digest(unit),
        },
        comparison_locks={},
        shot_dir=tmp_path,
        layer_id="1",
        assets_dir=None,
        feedback_policy={},
        mutation_roles=(),
        scope_baseline=set(),
        unit_scope={},
        _black_frame_note=lambda _row: "",
        _black_search_stop=lambda: "",
        _register_candidate=lambda _row: None,
        selected_authority=guard.selected_authority,
        attempt_guard=guard,
    )

    def publish_worklist() -> None:
        try:
            asyncio.run(worklist.handler({"items": ["finish exact ticket"]}))
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def replan() -> None:
        try:
            legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="revoke during worklist preparation",
                evidence=["fixture:side-file-race"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    publication_thread = Thread(target=publish_worklist)
    replan_thread = Thread(target=replan)
    publication_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on unbounded worklist staging"
    finally:
        release_prepare.set()
    publication_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    target = worklists.unit_worklist_path(
        tmp_path,
        layer_id="1",
        unit_id=unit.id,
        unit_hash=unit_state.unit_digest(unit),
    )
    assert not target.exists()
    assert not list(target.parent.glob(f".{target.name}.prepared.*"))


def test_side_file_target_symlink_fails_before_preparation(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-runtime.json"
    outside.write_text("[]\n", encoding="utf-8")
    (tmp_path / "runtime_checks.json").symlink_to(outside)
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="real regular file",
        ):
            runtime_check_publication.prepare_runtime_check_update(
                tmp_path,
                [_runtime_row("blocked")],
                authority_binding="work-unit-attempt:claim-a",
            )
        assert outside.read_text(encoding="utf-8") == "[]\n"
    finally:
        outside.unlink()


def test_side_file_fifo_target_fails_nonblocking_and_cleans_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not getattr(os, "O_NONBLOCK", 0):
        pytest.skip("platform does not expose nonblocking open")
    target = tmp_path / "result.json"
    os.mkfifo(target)
    registry = prepared_publication._registry
    pending_before = set(registry._PENDING_ACQUISITIONS)
    publications_before = set(registry._PUBLICATIONS)
    verifications_before = set(registry._VERIFICATIONS)
    real_open = os.open
    target_opened_nonblocking = False

    def checked_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal target_opened_nonblocking
        if os.fspath(path) == target.name and dir_fd is not None:
            target_opened_nonblocking = True
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(registry.os, "open", checked_open)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="real regular file",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            target.name,
            lambda _current: (b'{"generation":1}\n', None),
            authority_binding="work-unit-attempt:claim-a",
        )

    assert target_opened_nonblocking is True
    assert prepared_publication._READ_FLAGS & os.O_NONBLOCK
    assert prepared_publication._LOCK_FLAGS & os.O_NONBLOCK
    assert set(registry._PENDING_ACQUISITIONS) == pending_before
    assert set(registry._PUBLICATIONS) == publications_before
    assert set(registry._VERIFICATIONS) == verifications_before
    assert list(tmp_path.glob(".result.json.prepared.*")) == []


def _replay_destination() -> Path:
    return Path(
        "runs/fixture/checkpoints/layer-finalizations/"
        "lfc-fixture.group-0.replay.json"
    )


def _issue_replay_destination_authorization(
    shot: Path,
    destination: str | Path,
):
    return prepared_publication_destinations._issue_prepared_publication_destination_authorization(
        issuer=layer_replay_receipts._DESTINATION_ISSUER,
        shot_folder=shot,
        destination=destination,
    )


def _accept_prepared_destination(
    _authorization: object,
    _transaction_binding: object,
) -> None:
    return None


@pytest.mark.parametrize("mode", ["prepare", "publish", "absolute", "dotdot"])
def test_public_generic_publication_refuses_finalization_receipt_destination(
    tmp_path: Path,
    mode: str,
) -> None:
    destination = _replay_destination()
    selected: str | Path = destination
    if mode == "absolute":
        selected = tmp_path / destination
    elif mode == "dotdot":
        selected = destination.parent / "spare" / ".." / destination.name

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="requires its exact typed owner authorization",
    ):
        if mode == "publish":
            prepared_publication.publish_file_update(
                tmp_path,
                selected,
                lambda _current: (b"fixture\n", None),
                authority_binding="raw-finalization-bypass",
            )
        else:
            prepared_publication.prepare_file_update(
                tmp_path,
                selected,
                lambda _current: (b"fixture\n", None),
                authority_binding="raw-finalization-bypass",
            )

    assert not (tmp_path / destination).exists()


@pytest.mark.parametrize(
    ("root", "destination", "expected_family"),
    [
        (Path("plans"), Path("outcomes/forged.json"), "layer-outcome"),
        (Path("plans/outcomes"), Path("forged.json"), "layer-outcome"),
        (Path("build"), Path("layer_1.py"), "layer-artifact"),
        (
            Path("runs/fixture/checkpoints"),
            Path("layer-finalizations/lfc-forged.group-0.replay.json"),
            "layer-replay-receipt",
        ),
    ],
)
def test_caller_selected_subroot_cannot_hide_protected_destination(
    tmp_path: Path,
    root: Path,
    destination: Path,
    expected_family: str,
) -> None:
    selected_root = tmp_path / root
    selected_root.mkdir(parents=True)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match=rf"protected {expected_family} destination requires its exact typed owner authorization",
    ):
        prepared_publication.publish_file_update(
            selected_root,
            destination,
            lambda _current: (b"forged\n", None),
            authority_binding="caller-selected-subroot-bypass",
        )

    assert not (selected_root / destination).exists()


def test_caller_selected_ancestor_cannot_hide_protected_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / _replay_destination()

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="protected layer-replay-receipt destination requires its exact typed owner authorization",
    ):
        prepared_publication.prepare_file_update(
            tmp_path.parent,
            destination,
            lambda _current: (b"forged\n", None),
            authority_binding="caller-selected-ancestor-bypass",
        )

    assert not destination.exists()


def test_protected_noop_requires_owner_before_read_or_update(tmp_path: Path) -> None:
    destination = _replay_destination()
    target = tmp_path / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing\n")
    update_called = False

    def noop(_current: bytes | None) -> tuple[None, None]:
        nonlocal update_called
        update_called = True
        return None, None

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="requires its exact typed owner authorization",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            destination,
            noop,
            authority_binding="raw-finalization-noop",
        )

    assert update_called is False
    assert target.read_bytes() == b"existing\n"


@pytest.mark.parametrize(
    "endpoint",
    [
        Path("runs/fixture/checkpoints/layer-finalizations"),
        Path("build"),
        Path("build/units"),
    ],
)
def test_authority_namespace_endpoint_is_reserved(
    tmp_path: Path,
    endpoint: Path,
) -> None:

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="reserved authority namespace",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            endpoint,
            lambda _current: (b"squat\n", None),
            authority_binding="raw-finalization-directory-squat",
        )

    assert not (tmp_path / endpoint).exists()


def test_owner_authorization_is_exact_path_one_shot_and_policy_bound(
    tmp_path: Path,
) -> None:
    destination = _replay_destination()
    other = destination.with_name("lfc-other.group-0.replay.json")
    authorization = _issue_replay_destination_authorization(
        tmp_path,
        destination,
    )

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="another shot, path, or family",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            other,
            lambda _current: (b"other\n", None),
            authority_binding="typed-replay-other",
            commit_policy=_accept_prepared_destination,
            destination_authorization=authorization,
        )

    update = prepared_publication.prepare_file_update(
        tmp_path,
        destination,
        lambda _current: (b"replay\n", None),
        authority_binding="typed-replay-exact",
        commit_policy=_accept_prepared_destination,
        destination_authorization=authorization,
    )
    assert update.publication is not None
    prepared_publication.discard_prepared_file(update.publication)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="already consumed",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            destination,
            lambda _current: (b"replay\n", None),
            authority_binding="typed-replay-reuse",
            commit_policy=_accept_prepared_destination,
            destination_authorization=authorization,
        )

    without_policy = _issue_replay_destination_authorization(tmp_path, destination)
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="requires a stored commit policy",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            destination,
            lambda _current: (b"replay\n", None),
            authority_binding="typed-replay-without-policy",
            destination_authorization=without_policy,
        )

    evaluation_destination = destination.with_name("lfc-fixture.evaluation.json")
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="cannot authorize destination",
    ):
        _issue_replay_destination_authorization(
            tmp_path,
            evaluation_destination,
        )


def test_owner_authorization_noop_is_still_one_shot(tmp_path: Path) -> None:
    destination = _replay_destination()
    target = tmp_path / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing\n")
    authorization = _issue_replay_destination_authorization(tmp_path, destination)

    update = prepared_publication.prepare_file_update(
        tmp_path,
        destination,
        lambda current: (None, current),
        authority_binding="typed-replay-noop",
        commit_policy=_accept_prepared_destination,
        destination_authorization=authorization,
    )
    assert update.publication is None
    assert update.result == b"existing\n"

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="already consumed",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            destination,
            lambda current: (None, current),
            authority_binding="typed-replay-noop-reuse",
            commit_policy=_accept_prepared_destination,
            destination_authorization=authorization,
        )


def test_owner_authorization_refuses_cross_shot_and_thread_transfer(
    tmp_path: Path,
) -> None:
    destination = _replay_destination()
    authorization = _issue_replay_destination_authorization(tmp_path, destination)
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="another shot, path, or family",
    ):
        prepared_publication.prepare_file_update(
            foreign,
            destination,
            lambda _current: (b"foreign\n", None),
            authority_binding="typed-replay-foreign-shot",
            commit_policy=_accept_prepared_destination,
            destination_authorization=authorization,
        )

    failures: list[BaseException] = []

    def prepare_in_foreign_thread() -> None:
        try:
            prepared_publication.prepare_file_update(
                tmp_path,
                destination,
                lambda _current: (b"thread\n", None),
                authority_binding="typed-replay-foreign-thread",
                commit_policy=_accept_prepared_destination,
                destination_authorization=authorization,
            )
        except BaseException as exc:  # asserted below
            failures.append(exc)

    worker = Thread(target=prepare_in_foreign_thread)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(failures) == 1
    assert "another process or thread" in str(failures[0])

    update = prepared_publication.prepare_file_update(
        tmp_path,
        destination,
        lambda _current: (b"main\n", None),
        authority_binding="typed-replay-main-thread",
        commit_policy=_accept_prepared_destination,
        destination_authorization=authorization,
    )
    prepared_publication.discard_prepared_file(update.publication)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_owner_authorization_is_inert_after_fork_and_fresh_child_issue_works(
    tmp_path: Path,
) -> None:
    destination = _replay_destination()
    inherited = _issue_replay_destination_authorization(tmp_path, destination)
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no branch - parent asserts the child result
        os.close(read_descriptor)
        try:
            prepared_publication.prepare_file_update(
                tmp_path,
                destination,
                lambda _current: (b"inherited\n", None),
                authority_binding="typed-replay-inherited-child",
                commit_policy=_accept_prepared_destination,
                destination_authorization=inherited,
            )
        except prepared_publication.FilePublicationConflict:
            inherited_refused = True
        else:
            inherited_refused = False
        fresh = _issue_replay_destination_authorization(tmp_path, destination)
        update = prepared_publication.prepare_file_update(
            tmp_path,
            destination,
            lambda _current: (b"fresh\n", None),
            authority_binding="typed-replay-fresh-child",
            commit_policy=_accept_prepared_destination,
            destination_authorization=fresh,
        )
        prepared_publication.discard_prepared_file(update.publication)
        os.write(write_descriptor, b"ok" if inherited_refused else b"accepted")
        os.close(write_descriptor)
        os._exit(0)

    os.close(write_descriptor)
    observed = os.read(read_descriptor, 32)
    os.close(read_descriptor)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert observed == b"ok"

    parent_update = prepared_publication.prepare_file_update(
        tmp_path,
        destination,
        lambda _current: (b"parent\n", None),
        authority_binding="typed-replay-parent",
        commit_policy=_accept_prepared_destination,
        destination_authorization=inherited,
    )
    prepared_publication.discard_prepared_file(parent_update.publication)


@pytest.mark.parametrize(
    "destination",
    [Path("build/units/1/hero.py"), Path("scratch/diagnostic.py")],
)
def test_noncomposed_python_destination_remains_generic(
    tmp_path: Path,
    destination: Path,
) -> None:
    prepared_publication.publish_file_update(
        tmp_path,
        destination,
        lambda _current: (b"# fixture\n", None),
        authority_binding="ordinary-python-side-file",
    )

    assert (tmp_path / destination).read_bytes() == b"# fixture\n"


def test_raw_composed_layer_script_destination_is_protected(tmp_path: Path) -> None:
    destination = Path("build/layer_1.py")

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="layer-artifact destination requires its exact typed owner authorization",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            destination,
            lambda _current: (b"# forged\n", None),
            authority_binding="raw-layer-artifact",
        )

    assert not (tmp_path / destination).exists()


def test_physical_alias_cannot_bypass_protected_destination_family(
    tmp_path: Path,
) -> None:
    protected_parent = tmp_path / _replay_destination().parent
    protected_parent.mkdir(parents=True)
    alias = tmp_path / "receipt-alias"
    alias.symlink_to(protected_parent, target_is_directory=True)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match=r"real directory|real non-symlink directory",
    ):
        prepared_publication.prepare_file_update(
            tmp_path,
            alias / "lfc-forged.group-0.replay.json",
            lambda _current: (b"forged\n", None),
            authority_binding="raw-finalization-alias",
        )

    assert list(protected_parent.iterdir()) == []
