"""Tentative JIT-head selection with exact rollback and semantic postverification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)
from vfx_harness.orchestration.jit_materialization.schema import OVERLAY_ARTIFACTS
from vfx_harness.orchestration.jit_materialization.view_pointer import JitViewPointer

PointerJsonWriter = Callable[[Path, Path, Mapping[str, Any]], bytes]
PointerBytesWriter = Callable[[Path, Path, bytes], None]
PointerRemover = Callable[[Path, Path], None]


class SelectionHeads(Protocol):
    """Fields used from the strict two-head observation without importing its parser."""

    plan_pointer_bytes: bytes | None
    jit_pointer_bytes: bytes | None
    jit: JitViewPointer | None


SemanticVerifier = Callable[[Path, SelectionHeads, JitViewPointer], None]


def selected_authority(shot: Path):
    """Resolve the current fully verified materialization authority."""

    from vfx_harness.orchestration.authority_selection import (  # noqa: PLC0415
        SelectedAuthorityResolutionError,
        resolve_selected_authority,
    )

    try:
        selected = resolve_selected_authority(shot)
    except SelectedAuthorityResolutionError as exc:
        raise MaterializationSelectionConflict(
            f"selected materialization authority is invalid: {exc}"
        ) from exc
    if selected.plan is None:
        raise MaterializationSelectionConflict(
            "materialization requires selected global plan authority"
        )
    return selected


def resolve_authority_from_locked_heads(
    shot: Path,
    heads: SelectionHeads,
):
    """Fully verify already-read heads without recursively acquiring their lock."""

    from vfx_harness.orchestration.authority_selection import (  # noqa: PLC0415
        SelectedAuthorityResolutionError,
        resolve_selected_authority_from_heads,
    )

    try:
        return resolve_selected_authority_from_heads(shot, heads)
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(f"selected materialization authority is invalid: {exc}") from exc


def require_selected_jit_semantics(
    shot: Path,
    heads: SelectionHeads,
    pointer: JitViewPointer,
) -> None:
    """Prove one exact JIT head is the effective verified semantic authority."""

    selected = resolve_authority_from_locked_heads(shot, heads)
    bundle = selected.assertion.bundle
    effective = selected.assertion.effective_view
    if (
        selected.plan is None
        or selected.plan.revision != pointer.plan_revision
        or selected.plan.bundle.content_hash != pointer.bundle_hash
        or bundle is None
        or bundle.digest != pointer.bundle_hash
        or effective is None
        or effective.source != "jit"
        or effective.digest != pointer.view_hash
    ):
        raise ValueError(
            "selected JIT head did not resolve as the exact effective semantic authority"
        )
    expected_paths = {
        name: shot / pointer.artifacts[name] for name in OVERLAY_ARTIFACTS
    }
    if any(
        selected.artifact_paths.get(name) != path
        for name, path in expected_paths.items()
    ):
        raise ValueError(
            "selected JIT semantic authority resolved different artifact locators"
        )


def _postverify_jit_replacement(
    shot: Path,
    predecessor_heads: SelectionHeads,
    pointer: JitViewPointer,
    *,
    verify_semantics: SemanticVerifier,
) -> None:
    from vfx_harness.orchestration.authority_selection_heads import (  # noqa: PLC0415
        read_authority_selection_heads,
    )

    selected = read_authority_selection_heads(shot)
    if (
        selected.plan_pointer_bytes != predecessor_heads.plan_pointer_bytes
        or selected.jit != pointer
    ):
        raise ValueError(
            "tentative JIT replacement did not preserve the plan head and select "
            "the exact proposed JIT head"
        )
    verify_semantics(shot, selected, pointer)


def _restore_jit_predecessor(
    shot: Path,
    pointer_path: Path,
    predecessor_heads: SelectionHeads,
    *,
    operation: str,
    publication_error: BaseException,
    replace_pointer_bytes: PointerBytesWriter,
    remove_pointer: PointerRemover,
) -> None:
    from vfx_harness.orchestration.authority_selection_heads import (  # noqa: PLC0415
        read_authority_selection_heads,
    )

    try:
        if predecessor_heads.jit_pointer_bytes is None:
            remove_pointer(shot, pointer_path)
        else:
            replace_pointer_bytes(
                shot,
                pointer_path,
                predecessor_heads.jit_pointer_bytes,
            )
        restored = read_authority_selection_heads(shot)
        if (
            restored.plan_pointer_bytes != predecessor_heads.plan_pointer_bytes
            or restored.jit_pointer_bytes != predecessor_heads.jit_pointer_bytes
        ):
            raise ValueError(
                f"{operation} rollback did not restore the exact predecessor heads"
            )
    except BaseException as rollback_error:
        raise MaterializationSelectionConflict(
            f"{operation} selection conflict: tentative JIT head failed and its "
            f"exact predecessor could not be restored: {rollback_error}"
        ) from publication_error
    raise MaterializationSelectionConflict(
        f"{operation} selection conflict: tentative JIT head was rolled back: "
        f"{publication_error}"
    ) from publication_error


def replace_and_postverify_jit_pointer(
    shot: Path,
    pointer_path: Path,
    predecessor_heads: SelectionHeads,
    pointer: JitViewPointer,
    *,
    operation: str,
    replace_pointer_json: PointerJsonWriter,
    replace_pointer_bytes: PointerBytesWriter,
    remove_pointer: PointerRemover,
    verify_semantics: SemanticVerifier,
) -> None:
    """Tentatively replace, verify, or restore one JIT head under the EX lock."""

    try:
        replace_pointer_json(shot, pointer_path, pointer.as_dict())
        _postverify_jit_replacement(
            shot,
            predecessor_heads,
            pointer,
            verify_semantics=verify_semantics,
        )
    except BaseException as publication_error:
        _restore_jit_predecessor(
            shot,
            pointer_path,
            predecessor_heads,
            operation=operation,
            publication_error=publication_error,
            replace_pointer_bytes=replace_pointer_bytes,
            remove_pointer=remove_pointer,
        )
