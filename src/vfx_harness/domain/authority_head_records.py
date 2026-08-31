"""Pure closed projections of revisioned selected-authority head records."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

PLAN_POINTER_SCHEMA = "vfx-harness.plan-pointer/v2"
PLAN_CONSUMER_VIEW_SCHEMA = "vfx-harness.plan-consumer-view/v3"
AUTHORITY_SELECTION_TOKEN_SCHEMA = "vfx-harness.authority-selection-token/v1"
JIT_VIEW_POINTER_SCHEMA = "vfx-harness.jit-layer-view/v2"
JIT_STATE_DIR = Path("state/jit-layers")
JIT_CURRENT_PATH = JIT_STATE_DIR / "current.json"
OVERLAY_ARTIFACTS = (
    "layers.json",
    "scene_checks.json",
    "checks.json",
    "requirements.json",
    "acceptance.json",
)
PUBLISHABLE_OUTCOMES = frozenset(
    {"clean", "clean_with_assumptions", "clean_with_deferred"}
)
_PLAN_FIELDS = frozenset(
    {
        "schema",
        "revision",
        "run_id",
        "bundle",
        "content_hash",
        "outcome",
        "published_at",
    }
)
_TOKEN_FIELDS = frozenset(
    {
        "schema",
        "plan_revision",
        "plan_pointer_sha256",
        "jit_revision",
        "jit_pointer_sha256",
    }
)
_CONSUMER_FIELDS = frozenset(
    {
        "schema",
        "shot",
        "bundle",
        "content_hash",
        "base_selection",
        "effective_view",
        "authored_inputs",
        "decision_inputs",
    }
)
_VIEW_FIELDS = frozenset({"source", "digest", "artifact_hashes"})
_DECISION_INPUT_FIELDS = frozenset({"size", "sha256"})
_DECISION_INPUT_PATHS = frozenset(
    {
        "plan_amendments.jsonl",
        "state/plan-resolutions.jsonl",
    }
)


class AuthorityHeadRecordError(ValueError):
    """A selected-authority head projection is ambiguous or producer-invalid."""


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def decode_json_object(payload: bytes, where: str) -> dict[str, Any]:
    """Decode one UTF-8 JSON object while refusing repeated members."""

    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJsonKey as exc:
        raise AuthorityHeadRecordError(
            f"{where} contains duplicate JSON key {exc.args[0]!r}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityHeadRecordError(f"{where} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AuthorityHeadRecordError(f"{where} must contain an object")
    return value


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode the sole accepted producer representation of a selected head record."""

    if not isinstance(value, Mapping):
        raise AuthorityHeadRecordError("authority head JSON must be an object")
    try:
        return (
            json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityHeadRecordError(
            "authority head JSON must be finite and serializable"
        ) from exc


def decode_canonical_json_object(payload: bytes, where: str) -> dict[str, Any]:
    """Decode one strict object and require the producer's exact canonical bytes."""

    value = decode_json_object(payload, where)
    if payload != canonical_json_bytes(value):
        raise AuthorityHeadRecordError(f"{where} bytes are not canonical")
    return value


def _digest(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuthorityHeadRecordError(
            f"{where} must be a lowercase SHA-256 digest"
        )
    return value


def _positive_revision(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AuthorityHeadRecordError(f"{where} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class AuthoritySelectionTokenProjection:
    plan_revision: int
    plan_pointer_sha256: str | None
    jit_revision: int
    jit_pointer_sha256: str | None


def parse_authority_selection_token(
    value: object,
    where: str = "authority selection token",
) -> AuthoritySelectionTokenProjection:
    if not isinstance(value, Mapping):
        raise AuthorityHeadRecordError(f"{where} must be an object")
    found = set(value)
    if found != _TOKEN_FIELDS:
        raise AuthorityHeadRecordError(
            f"{where} fields mismatch; missing={sorted(_TOKEN_FIELDS - found)}; "
            f"unexpected={sorted(found - _TOKEN_FIELDS)}"
        )
    if value.get("schema") != AUTHORITY_SELECTION_TOKEN_SCHEMA:
        raise AuthorityHeadRecordError(
            f"{where}.schema must be {AUTHORITY_SELECTION_TOKEN_SCHEMA!r}, "
            f"found {value.get('schema')!r}"
        )
    revisions: dict[str, int] = {}
    digests: dict[str, str | None] = {}
    for head in ("plan", "jit"):
        revision = value.get(f"{head}_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise AuthorityHeadRecordError(
                f"{where}.{head}_revision must be a non-negative integer"
            )
        pointer_digest = value.get(f"{head}_pointer_sha256")
        if revision == 0:
            if pointer_digest is not None:
                raise AuthorityHeadRecordError(
                    f"{where} absent {head} head at revision 0 requires a null "
                    "pointer digest"
                )
        elif pointer_digest is None:
            raise AuthorityHeadRecordError(
                f"{where} present {head} head requires a pointer digest"
            )
        else:
            pointer_digest = _digest(
                pointer_digest,
                f"{where}.{head}_pointer_sha256",
            )
        revisions[head] = revision
        digests[head] = pointer_digest
    return AuthoritySelectionTokenProjection(
        plan_revision=revisions["plan"],
        plan_pointer_sha256=digests["plan"],
        jit_revision=revisions["jit"],
        jit_pointer_sha256=digests["jit"],
    )


@dataclass(frozen=True, slots=True)
class PlanPointerProjection:
    revision: int
    run_id: str
    bundle: str
    content_hash: str
    outcome: str
    published_at: str


def parse_plan_pointer(value: object) -> PlanPointerProjection:
    if not isinstance(value, Mapping):
        raise AuthorityHeadRecordError("plan pointer must contain an object")
    found = set(value)
    if found != _PLAN_FIELDS:
        raise AuthorityHeadRecordError(
            "plan pointer fields mismatch; "
            f"missing={sorted(_PLAN_FIELDS - found)}; "
            f"unexpected={sorted(found - _PLAN_FIELDS)}"
        )
    if value.get("schema") != PLAN_POINTER_SCHEMA:
        raise AuthorityHeadRecordError(
            f"unsupported plan pointer schema: {value.get('schema')!r}"
        )
    revision = _positive_revision(value.get("revision"), "plan pointer.revision")
    run_id = value.get("run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
    ):
        raise AuthorityHeadRecordError("plan pointer.run_id is invalid")
    content_hash = _digest(value.get("content_hash"), "plan pointer.content_hash")
    outcome = value.get("outcome")
    if outcome not in PUBLISHABLE_OUTCOMES:
        raise AuthorityHeadRecordError("plan pointer.outcome is not publishable")
    published_at = value.get("published_at")
    if not isinstance(published_at, str) or not published_at.strip():
        raise AuthorityHeadRecordError("plan pointer.published_at must be non-empty")
    bundle = value.get("bundle")
    expected = f"runs/{run_id}/checkpoints/plans/bundles/{content_hash}"
    if (
        not isinstance(bundle, str)
        or PurePosixPath(bundle).as_posix() != bundle
        or bundle != expected
    ):
        raise AuthorityHeadRecordError("plan pointer.bundle is not its exact digest root")
    return PlanPointerProjection(
        revision=revision,
        run_id=run_id,
        bundle=bundle,
        content_hash=content_hash,
        outcome=str(outcome),
        published_at=published_at,
    )


@dataclass(frozen=True, slots=True)
class PlanConsumerViewProjection:
    shot: Path
    bundle: Path
    content_hash: str
    base_selection: AuthoritySelectionTokenProjection
    view_source: str
    view_digest: str
    artifact_hashes: dict[str, str]
    authored_inputs: dict[str, str]
    decision_inputs: dict[str, dict[str, str | int]]

    @property
    def bundle_run_id(self) -> str:
        return self.bundle.relative_to(self.shot / "runs").parts[0]


def parse_plan_consumer_view(value: object) -> PlanConsumerViewProjection:
    if not isinstance(value, Mapping) or set(value) != _CONSUMER_FIELDS:
        raise AuthorityHeadRecordError(
            "plan consumer view marker fields do not match the v3 schema"
        )
    if value.get("schema") != PLAN_CONSUMER_VIEW_SCHEMA:
        raise AuthorityHeadRecordError("plan consumer view marker schema is unsupported")
    raw_shot = value.get("shot")
    raw_bundle = value.get("bundle")
    if (
        not isinstance(raw_shot, str)
        or not Path(raw_shot).is_absolute()
        or Path(raw_shot) != Path(os.path.abspath(raw_shot))
    ):
        raise AuthorityHeadRecordError("plan consumer view marker.shot must be absolute")
    if (
        not isinstance(raw_bundle, str)
        or not Path(raw_bundle).is_absolute()
        or Path(raw_bundle) != Path(os.path.abspath(raw_bundle))
    ):
        raise AuthorityHeadRecordError("plan consumer view marker.bundle must be absolute")
    shot = Path(raw_shot)
    bundle = Path(raw_bundle)
    content_hash = _digest(value.get("content_hash"), "marker.content_hash")
    try:
        relative = bundle.relative_to(shot / "runs")
    except ValueError as exc:
        raise AuthorityHeadRecordError("marker.bundle escapes the shot run store") from exc
    if (
        len(relative.parts) != 5
        or relative.parts[1:4] != ("checkpoints", "plans", "bundles")
        or relative.parts[4] != content_hash
        or relative.parts[0] in {"", ".", ".."}
    ):
        raise AuthorityHeadRecordError("marker.bundle is not its exact run-owned digest root")
    base_selection = parse_authority_selection_token(
        value.get("base_selection"),
        "plan consumer view marker.base_selection",
    )
    if base_selection.plan_revision == 0:
        raise AuthorityHeadRecordError(
            "plan consumer view marker requires a present plan head"
        )
    raw_view = value.get("effective_view")
    if not isinstance(raw_view, Mapping) or set(raw_view) != _VIEW_FIELDS:
        raise AuthorityHeadRecordError("marker.effective_view fields do not match v3")
    source = raw_view.get("source")
    if source not in {"bundle", "jit"}:
        raise AuthorityHeadRecordError("marker.effective_view.source is invalid")
    view_digest = _digest(raw_view.get("digest"), "marker.effective_view.digest")
    if source == "bundle" and view_digest != content_hash:
        raise AuthorityHeadRecordError("bundle-backed marker view digest must equal bundle")
    raw_hashes = raw_view.get("artifact_hashes")
    if not isinstance(raw_hashes, Mapping) or set(raw_hashes) != set(OVERLAY_ARTIFACTS):
        raise AuthorityHeadRecordError("marker must hash every overlay artifact exactly once")
    raw_authored = value.get("authored_inputs")
    if not isinstance(raw_authored, Mapping) or "brief.md" not in raw_authored:
        raise AuthorityHeadRecordError(
            "marker.authored_inputs must be a map containing brief.md"
        )
    authored_inputs: dict[str, str] = {}
    for raw_name, raw_digest in raw_authored.items():
        if not isinstance(raw_name, str):
            raise AuthorityHeadRecordError(
                "marker.authored_inputs paths must be strings"
            )
        path = PurePosixPath(raw_name)
        if (
            path.as_posix() != raw_name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or (
                raw_name != "brief.md"
                and not (len(path.parts) > 1 and path.parts[0] == "refs")
            )
        ):
            raise AuthorityHeadRecordError(
                f"marker.authored_inputs contains unsupported path {raw_name!r}"
            )
        authored_inputs[raw_name] = _digest(
            raw_digest,
            f"marker.authored_inputs[{raw_name!r}]",
        )
    raw_decisions = value.get("decision_inputs")
    if not isinstance(raw_decisions, Mapping):
        raise AuthorityHeadRecordError("marker.decision_inputs must be an object")
    unsupported_decisions = sorted(set(raw_decisions) - _DECISION_INPUT_PATHS)
    if unsupported_decisions:
        raise AuthorityHeadRecordError(
            "marker.decision_inputs names unsupported paths: "
            + ", ".join(unsupported_decisions)
        )
    decision_inputs: dict[str, dict[str, str | int]] = {}
    for raw_name, raw_identity in raw_decisions.items():
        if not isinstance(raw_identity, Mapping) or set(raw_identity) != _DECISION_INPUT_FIELDS:
            raise AuthorityHeadRecordError(
                f"marker.decision_inputs[{raw_name!r}] fields are invalid"
            )
        size = raw_identity.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AuthorityHeadRecordError(
                f"marker.decision_inputs[{raw_name!r}].size must be a non-negative integer"
            )
        decision_inputs[str(raw_name)] = {
            "size": size,
            "sha256": _digest(
                raw_identity.get("sha256"),
                f"marker.decision_inputs[{raw_name!r}].sha256",
            ),
        }
    return PlanConsumerViewProjection(
        shot=shot,
        bundle=bundle,
        content_hash=content_hash,
        base_selection=base_selection,
        view_source=str(source),
        view_digest=view_digest,
        artifact_hashes={
            name: _digest(raw_hashes[name], f"marker artifact {name!r}")
            for name in OVERLAY_ARTIFACTS
        },
        authored_inputs=authored_inputs,
        decision_inputs=decision_inputs,
    )
