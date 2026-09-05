"""Canonical, machine-readable storage for one harness invocation.

The shot root contains authored inputs and the currently accepted build. Generated output
belongs to a run. Keeping that distinction explicit prevents transcripts, renders, temporary
Blender files, checkpoints, and reports from different attempts from becoming one directory
that an agent has to reverse-engineer.

`VFXH_RUN_DIR` is inherited by stage subprocesses. A direct stage invocation creates its own
structured run instead of writing ad hoc shot-root output.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.environment_results import EnvironmentResult
from vfx_harness.domain.run_status import RUN_STATUS_SCHEMA, RunStatusV2
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import (
    StopCause,
    StopEnvelope,
    StopIdentity,
)
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EngineeringRouteCommitted,
    RouteEngineeringTarget,
    StopAction,
)
from vfx_harness.observability import unclassified_authority
from vfx_harness.observability.run_owner_fence import RunOwnerFenceError, read_run_owner_claim
from vfx_harness.observability.run_owner_manifest import (
    RUN_DISPATCH_SCHEMA,
    RUN_MANIFEST_LAYOUT,
    RUN_MANIFEST_SCHEMA,
    RunOwnerManifestError,
    parse_run_owner_manifest,
)
from vfx_harness.observability.runid import RUN_ID

ENV = "VFXH_RUN_DIR"
SCHEMA = RUN_MANIFEST_SCHEMA
LATEST_SCHEMA = "vfx-harness.latest-run/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_STATUS_FIELDS = {"schema", "run_id", "state", "updated_at", "exit_code", "detail"}

# Integer SystemExit codes stringify to the digit ("7"), which is truthy and used to
# become status.json detail. Map the digit back to the meaning the driver already has.
EXIT_DETAILS = {
    3: "TRUNCATED — raise the budget or split the layer",
    4: "CHAIN BROKEN — a prior layer's script no longer composes",
    5: "unanswered plan questions — settle them first",
    6: "UNACCEPTED PRIOR — a lower layer must pass first",
    7: "INCOMPLETE CHAIN",
    8: "plan is STALE against brief.md — re-plan",
    9: "layer ran cleanly but its VERDICT was not a pass",
}


class RequestedExit(SystemExit):
    """Integer process exit that still carries a human detail for status.json."""

    def __init__(self, code: int, detail: str, *, terminal_cause: str = "requested_exit"):
        self.detail = str(detail).strip() or EXIT_DETAILS.get(int(code), f"exit {code}")
        self.terminal_cause = terminal_cause
        super().__init__(int(code))

    def __str__(self) -> str:
        return self.detail


class TypedStop(SystemExit):
    """Non-zero process exit carrying the boundary's complete typed stop authority."""

    def __init__(self, code: int, envelope: StopEnvelope):
        if not isinstance(code, int) or isinstance(code, bool) or code == 0:
            raise ValueError("TypedStop.code must be a non-zero integer")
        if not isinstance(envelope, StopEnvelope):
            raise ValueError("TypedStop.envelope must be a StopEnvelope")
        self.stop_envelope = envelope
        self.detail = f"{envelope.stop_class}: {envelope.found} {envelope.next_action}"
        self.terminal_cause = envelope.stop_class
        super().__init__(code)

    def __str__(self) -> str:
        return self.detail


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _typed_evidence_digest(
    evidence: StopEvidenceRef,
    value: Any,
    *,
    where: str,
) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a typed JSON object")
    if value.get("schema") != evidence.record_schema:
        raise ValueError(f"{where} schema mismatch; expected {evidence.record_schema!r}, found {value.get('schema')!r}")
    if evidence.record_schema == EnvironmentResult.SCHEMA:
        return EnvironmentResult.from_dict(value, where).digest
    digest_field = next(
        (name for name in ("record_digest", "result_digest") if name in value),
        None,
    )
    if digest_field is None:
        return canonical_digest(value)
    if value[digest_field] != evidence.record_digest:
        raise ValueError(
            f"{where}.{digest_field} is stale; expected {evidence.record_digest!r}, found {value[digest_field]!r}"
        )
    return canonical_digest({key: item for key, item in value.items() if key != digest_field})


def _validate_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not _SAFE_ID.fullmatch(value) or value in {".", "..", "latest"}:
        raise ValueError(f"invalid run id: {run_id!r}")
    return value


@dataclass(frozen=True, slots=True)
class RunLayout:
    shot: Path
    run_id: str
    root: Path
    # A stage may learn terminal authority before the invocation context exits. Keeping
    # these structured fields beside the layout lets the context publish them atomically
    # with status.json/summary.json instead of reducing them to an exception string.
    terminal_metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def inventory(self) -> Path:
        return self.root / "artifacts.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints"

    @property
    def scratch(self) -> Path:
        return self.root / "scratch"

    @property
    def deliverables(self) -> Path:
        return self.root / "deliverables"

    @property
    def stop_envelope(self) -> Path:
        return self.reports / "stop-envelope.json"

    def relative(self, path: str | Path) -> str:
        return Path(path).resolve().relative_to(self.shot).as_posix()

    def write_inventory(self) -> Path:
        """Publish a compact file catalog so readers never have to infer artifact roles."""
        rows = []
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            if path == self.inventory or ".tmp." in path.name:
                continue
            rel = path.relative_to(self.root).as_posix()
            top = rel.split("/", 1)[0]
            rows.append(
                {
                    "path": rel,
                    "category": top
                    if top in {"logs", "reports", "evidence", "checkpoints", "scratch", "deliverables"}
                    else "run-metadata",
                    "bytes": path.stat().st_size,
                    "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                }
            )
        _atomic_json(
            self.inventory,
            {
                "schema": "vfx-harness.artifact-index/v1",
                "run_id": self.run_id,
                "generated_at": _now(),
                "artifacts": rows,
            },
        )
        return self.inventory

    def write_summary(self, value: dict[str, Any]) -> Path:
        out = self.reports / "summary.json"
        _atomic_json(out, value)
        return out

    def write_report(self, name: str, value: dict[str, Any]) -> Path:
        """Publish one named structured report inside this invocation."""
        if not _SAFE_ID.fullmatch(name):
            raise ValueError(f"invalid report name: {name!r}")
        out = self.reports / f"{name}.json"
        _atomic_json(out, value)
        return out

    def write_evidence(self, namespace: str, name: str, value: dict[str, Any]) -> Path:
        """Publish one named structured evidence record under this run's evidence tree."""
        for label, part in (("evidence namespace", namespace), ("evidence name", name)):
            if not _SAFE_ID.fullmatch(part):
                raise ValueError(f"invalid {label}: {part!r}")
        out = self.evidence / namespace / f"{name}.json"
        _atomic_json(out, value)
        return out

    def write_stop_envelope(self, envelope: StopEnvelope) -> Path:
        """Publish and read back the run's one immutable terminal stop envelope."""
        if not isinstance(envelope, StopEnvelope):
            raise ValueError("stop envelope must be a StopEnvelope")
        if envelope.identity.run_id != self.run_id:
            raise ValueError(
                f"stop envelope run identity mismatch; expected {self.run_id!r}, found {envelope.identity.run_id!r}"
            )
        if self.stop_envelope.exists():
            current = self.read_stop_envelope()
            if current.digest != envelope.digest:
                raise ValueError(
                    "run already has a different immutable stop envelope; "
                    f"current={current.digest}, proposed={envelope.digest}"
                )
            return self.stop_envelope
        _atomic_json(self.stop_envelope, envelope.as_dict())
        published = self.read_stop_envelope()
        if published.digest != envelope.digest:
            raise ValueError("published stop envelope failed digest read-back")
        return self.stop_envelope

    def read_stop_envelope(self, *, expected_digest: str | None = None) -> StopEnvelope:
        """Read one strict envelope; malformed or stale terminal authority fails closed."""
        try:
            value = json.loads(self.stop_envelope.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"no readable stop envelope at {self.stop_envelope}") from exc
        envelope = StopEnvelope.from_dict(value, "stop-envelope")
        if expected_digest is not None and envelope.digest != expected_digest:
            raise ValueError(
                "stop envelope digest does not match terminal status; "
                f"expected {expected_digest}, found {envelope.digest}"
            )
        if envelope.identity.run_id != self.run_id:
            raise ValueError(
                f"stop envelope names another run; expected {self.run_id!r}, found {envelope.identity.run_id!r}"
            )
        return envelope

    def _verify_stop_evidence(self, evidence: StopEvidenceRef) -> None:
        try:
            shot_root = self.shot.resolve()
            path = (shot_root / evidence.locator).resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"stop evidence locator cannot be resolved: {evidence.locator!r}") from exc
        try:
            path.relative_to(shot_root)
        except ValueError as exc:
            raise ValueError(f"stop evidence locator escapes the shot root: {evidence.locator!r}") from exc
        if not path.is_file():
            raise ValueError(f"cited stop evidence is missing: {evidence.locator!r}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cited stop evidence is unreadable: {evidence.locator!r}") from exc
        observed_sha256 = hashlib.sha256(payload).hexdigest()
        if observed_sha256 != evidence.sha256:
            raise ValueError(
                "cited stop evidence SHA-256 mismatch; "
                f"locator={evidence.locator!r}, expected={evidence.sha256}, "
                f"found={observed_sha256}"
            )
        if evidence.record_schema is None:
            return
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cited typed stop evidence is malformed JSON: {evidence.locator!r}") from exc
        observed_digest = _typed_evidence_digest(
            evidence,
            value,
            where=f"stop evidence {evidence.locator!r}",
        )
        if observed_digest != evidence.record_digest:
            raise ValueError(
                "cited stop evidence record digest mismatch; "
                f"locator={evidence.locator!r}, expected={evidence.record_digest}, "
                f"found={observed_digest}"
            )

    def read_prepared_stop(self) -> StopEnvelope:
        """Read the stop envelope a stage prepared and verify every cited evidence byte.

        A stage inherited inside a driver run publishes only this typed envelope; the
        root owner consumes it here before selecting the terminal status itself.
        """

        envelope = self.read_stop_envelope()
        for evidence in envelope.evidence_refs:
            self._verify_stop_evidence(evidence)
        return envelope

    def read_terminal_stop(self) -> StopEnvelope:
        """Resolve terminal failure authority and every cited evidence byte fail closed."""

        try:
            status = json.loads(self.status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"no readable terminal status at {self.status}") from exc
        if not isinstance(status, dict) or status.get("schema") != RUN_STATUS_SCHEMA:
            raise ValueError("terminal status has an unsupported run-status schema")
        if status.get("run_id") != self.run_id:
            raise ValueError("terminal status names another run")
        state = status.get("state")
        if state == "interrupted":
            raise ValueError(
                "an interrupted run selects no stop envelope; read it through the interruption reader"
            )
        if state != "failed":
            raise ValueError(f"run status is not terminally unaccepted: {state!r}")
        digest = status.get("stop_envelope_digest")
        if not isinstance(digest, str):
            raise ValueError("terminal status has no stop-envelope digest")
        envelope = self.read_stop_envelope(expected_digest=digest)
        owner_digest = status.get("owner_claim_digest")
        if not isinstance(owner_digest, str):
            raise ValueError("terminal status retains no owner claim digest")
        try:
            owner = read_run_owner_claim(self.root, run_id=self.run_id, expected_digest=owner_digest)
        except RunOwnerFenceError as exc:
            raise ValueError(f"terminal status owner claim cannot be source-verified: {exc}") from exc
        RunStatusV2.from_dict(status, selected_record=envelope, owner=owner, where="terminal status")
        for evidence in envelope.evidence_refs:
            self._verify_stop_evidence(evidence)
        return envelope


def create(
    shot_folder: str | Path,
    run_id: str,
    *,
    command: str = "plan",
    dispatch_kind: str = "direct",
    shot_id: str | None = None,
    arguments: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
) -> RunLayout:
    """Create one prepared v2 run: its directories, closed manifest, and inventory.

    The manifest's typed dispatch discriminant is the only source of the run's public
    command and owner kind; it is validated through the same strict parser the owner
    claim reader uses.  A prepared run has no owner claim and no status until the root
    owner boundary acquires its fence and publishes ``running`` (HIR-0172).
    """

    shot = Path(shot_folder).expanduser().resolve()
    rid = _validate_run_id(run_id)
    layout = RunLayout(shot=shot, run_id=rid, root=shot / "runs" / rid)
    for folder in (
        layout.logs,
        layout.reports / "layers",
        layout.evidence / "renders",
        layout.evidence / "comparisons",
        layout.checkpoints / "blender",
        layout.checkpoints / "scripts",
        layout.scratch / "blender",
        layout.deliverables,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    os.environ[ENV] = str(layout.root)
    manifest = {
        "schema": SCHEMA,
        "run_id": rid,
        "shot_id": shot_id or shot.name,
        "started_at": _now(),
        "invocation": {
            "dispatch": {
                "schema": RUN_DISPATCH_SCHEMA,
                "kind": dispatch_kind,
                "command": command,
            },
            "argv": [f"vfx {command}", *(arguments if arguments else [str(shot)])],
            "parameters": dict(parameters or {}),
        },
        "layout": dict(RUN_MANIFEST_LAYOUT),
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
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        parse_run_owner_manifest(payload, run_id=rid)
    except RunOwnerManifestError as exc:
        raise ValueError(f"run manifest is not a valid {SCHEMA} owner manifest: {exc}") from exc
    _atomic_json(layout.manifest, manifest)
    layout.write_inventory()
    return layout


_STAGE_OWNER_COMMANDS = {
    "plan": "plan",
    "plan-layer": "plan",
    "plan-lab": "plan",
    "direct-stage": "plan",
    "build": "build",
    "layer-report": "build",
    "blender-session": "build",
    "script-checkpoint": "build",
    "metrics-feedback": "build",
    "accept": "accept",
    "acceptance": "accept",
    "render": "render",
}


def ensure(
    shot_folder: str | Path, run_id: str | None = None, *, shot_id: str | None = None, command: str = "direct-stage"
) -> RunLayout:
    """Return the inherited run, or create a prepared run for a stage outside a boundary.

    ``command`` is the stage's closed name; it maps to the public owner command the
    manifest dispatch records.  An unknown stage name fails closed.
    """

    current = active(shot_folder)
    if current:
        return current
    owner_command = _STAGE_OWNER_COMMANDS.get(command)
    if owner_command is None:
        raise ValueError(
            f"stage {command!r} has no public owner command; expected one of "
            f"{sorted(_STAGE_OWNER_COMMANDS)}"
        )
    if run_id is None:
        run_id = RUN_ID
    return create(shot_folder, run_id, command=owner_command, shot_id=shot_id)


def _unclassified_authoritative_state(layout: RunLayout, command: str) -> dict[str, Any]:
    """Return only semantic selected authority and durable accepted state."""

    state, _audit = unclassified_authority.snapshot(
        layout.shot,
        layout.manifest,
        boundary=command,
    )
    return state


def _exception_label(exc: BaseException, *, limit: int = 240) -> str:
    """`module.QualName: message`, whitespace-normalised and bounded.

    One value for the audit record and the operator-facing prose, so the envelope
    cannot describe an exception differently from the file beside it (HIR-0226).
    """
    qualified = f"{type(exc).__module__}.{type(exc).__qualname__}"
    message = " ".join(str(exc).split())
    if len(message) > limit:
        message = message[: limit - 1].rstrip() + "\u2026"
    return f"{qualified}: {message}" if message else qualified


def _unclassified_stop_envelope(
    layout: RunLayout,
    command: str,
    exc: BaseException,
    *,
    code: int,
    terminal_cause: str,
) -> StopEnvelope:
    """Fail closed when an owning boundary returned no typed classification.

    This classifies only the harness invariant that the envelope is missing. It does
    not infer retry, replan, or recovery authority from the exception, exit code, or
    prose that exposed the omission.
    """
    authoritative_state, authority_audit = unclassified_authority.snapshot(
        layout.shot,
        layout.manifest,
        boundary=command,
    )
    authoritative_before_digest = canonical_digest(authoritative_state)
    terminal_cause_id = unclassified_authority.closed_terminal_cause(terminal_cause)
    # Identity and authority are two questions. The stop authorizes nothing either way,
    # which is what the constant facts were protecting; but a constant identity meant
    # every unclassified boundary in every shot shared one fingerprint and one finding
    # id. Five genuinely different causes collided on one value -- an unhandled mint
    # ValueError, a stale finalization claim, a builder truncation, an operator's
    # SIGTERM, and an unclaimable-state conflict -- and the controller refuses a cause
    # fingerprint already dispatched in the shot, so the second real defect would be
    # suppressed as a repeat of the first. The cause is closed vocabulary and the
    # exception is its type only, so neither admits free-form prose (HIR-0214).
    normalized_facts = {
        "schema": "vfx-harness.unclassified-boundary-facts/v2",
        "boundary": command,
        "invariant": "terminal_boundary_requires_typed_stop",
        "terminal_cause": terminal_cause_id,
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "operator_initiated": terminal_cause_id in unclassified_authority.OPERATOR_CAUSES,
    }
    classification_digest = canonical_digest(normalized_facts)
    attempt_digest = canonical_digest(
        {
            "schema": "vfx-harness.unclassified-boundary-attempt/v1",
            "boundary": command,
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "legacy_terminal_cause": terminal_cause_id,
            "exit_code": code,
            "authoritative_before_digest": authoritative_before_digest,
        }
    )
    artifact_state_digest = canonical_digest(
        {
            "schema": "vfx-harness.unclassified-boundary-state/v1",
            "authority": authoritative_state,
        }
    )
    defect_document = {
        "schema": "vfx-harness.unclassified-boundary-defect/v1",
        "boundary": command,
        "invariant": "terminal_boundary_requires_typed_stop",
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "legacy_terminal_cause": terminal_cause_id,
        "exit_code": code,
        "authoritative_state": authoritative_state,
        "attempt_evidence_digest": attempt_digest,
        "classification_evidence_digest": classification_digest,
        "artifact_state_digest": artifact_state_digest,
    }
    audit_path = layout.write_report(
        "unclassified-boundary-audit",
        {
            "schema": "vfx-harness.unclassified-boundary-audit/v1",
            "run_id": layout.run_id,
            "boundary": command,
            "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "exception_message": str(exc),
            "legacy_terminal_cause": terminal_cause,
            "exit_code": code,
            "authority_sources": authority_audit,
        },
    )
    audit_locator = audit_path.relative_to(layout.root).as_posix()
    layout.terminal_metadata.update(
        {
            "unclassified_boundary_audit": audit_locator,
        }
    )
    defect_path = layout.write_report("unclassified-boundary-defect", defect_document)
    try:
        observed = json.loads(defect_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as read_exc:
        raise RuntimeError("unclassified-boundary defect evidence failed read-back") from read_exc
    defect_digest = canonical_digest(defect_document)
    if observed != defect_document or canonical_digest(observed) != defect_digest:
        raise RuntimeError("unclassified-boundary defect evidence changed during publication")
    authority_after, _audit_after = unclassified_authority.snapshot(
        layout.shot,
        layout.manifest,
        boundary=command,
    )
    if authority_after != authoritative_state:
        raise RuntimeError(
            "selected authority changed while unclassified-boundary evidence was published"
        )
    evidence = StopEvidenceRef(
        kind="stop_evidence",
        locator=defect_path.relative_to(layout.shot).as_posix(),
        sha256=hashlib.sha256(defect_path.read_bytes()).hexdigest(),
        record_schema=defect_document["schema"],
        record_digest=defect_digest,
    )
    cause = StopCause(
        invariant_id="terminal_boundary_requires_typed_stop",
        finding_ids=(f"unclassified-boundary-{classification_digest[:20]}",),
        owner_scope_ids=("observability",),
        normalized_facts_digest=classification_digest,
    )
    defect = EvidenceRecordAssertion(
        record_kind="defect",
        record_id=f"unclassified-boundary-{defect_digest[:20]}",
        evidence=evidence,
    )
    target = RouteEngineeringTarget(
        cause_fingerprint=cause.fingerprint_for("harness_defect"),
        attempt_evidence_digest=attempt_digest,
        owner_scope_ids=cause.owner_scope_ids,
        defect_record=defect,
        evidence=(evidence,),
        sink_id="engineering_handoff",
    )
    action = StopAction(
        target=target,
        postcondition=EngineeringRouteCommitted(
            defect_packet_digest=defect_digest,
            sink_id=target.sink_id,
            owner_scope_ids=target.owner_scope_ids,
        ),
    )
    return StopEnvelope(
        stage="infrastructure",
        stop_class="harness_defect",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=None,
            view_digest=None,
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=None,
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=cause,
        attempt_evidence_digest=attempt_digest,
        classification_evidence_digest=classification_digest,
        artifact_state_digest=artifact_state_digest,
        authoritative_before_digest=authoritative_before_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="unclassified-boundary",
        expected="Every unaccepted run boundary publishes a typed stop before returning.",
        # The exception is already recorded verbatim in the audit beside this envelope.
        # Naming only the boundary here discarded it at the one surface an operator
        # reads, because `detail` is f"{stop_class}: {found} {next_action}". Four
        # distinct causes were lost this way in one day, including a
        # LayerFinalizationConflict whose own message names the `vfx finalizations
        # release` transaction that recovers it (HIR-0226).
        found=(
            f"The {command!r} boundary returned without typed stop authority; it raised "
            f"{_exception_label(exc)}"
        ),
        next_action=(
            "Read that exception first -- it is the cause, and several of these name "
            f"their own owning boundary or recovery transaction. {audit_locator} holds "
            "it verbatim. Then route the boundary and exact attempt evidence to "
            "engineering."
        ),
    )


def missing_boundary_stop(
    layout: RunLayout,
    command: str,
    *,
    exit_code: int,
) -> StopEnvelope:
    """Classify the driver's observed absence of child stop authority, and nothing else."""
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0:
        raise ValueError("missing-boundary stop requires a non-zero child exit code")
    return _unclassified_stop_envelope(
        layout,
        command,
        RuntimeError("child boundary returned without a selected typed stop envelope"),
        code=exit_code,
        terminal_cause="missing_stop_envelope",
    )


def publish_exception_stop(
    layout: RunLayout,
    command: str,
    exc: BaseException,
    *,
    code: int,
    terminal_cause: str,
) -> StopEnvelope:
    candidate = getattr(exc, "stop_envelope", None)
    if candidate is None:
        candidate = _unclassified_stop_envelope(
            layout,
            command,
            exc,
            code=code,
            terminal_cause=terminal_cause,
        )
    elif not isinstance(candidate, StopEnvelope):
        raise ValueError("terminal exception stop_envelope is not a StopEnvelope")
    layout.write_stop_envelope(candidate)
    return candidate


def terminal_record(exc: BaseException) -> tuple[str, int, str, str]:
    """Classify a failed invocation without reducing distinct stops to exit code 1."""
    if isinstance(exc, KeyboardInterrupt):
        # A cancellation with no recorded signal intent is not an operator interruption.
        return "failed", 130, "cancelled_without_intent", "cancelled without a recorded signal intent"

    code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
    cause = str(getattr(exc, "terminal_cause", "") or "")
    metadata = getattr(exc, "run_metadata", {})
    outcome = str(metadata.get("outcome") or "") if isinstance(metadata, dict) else ""
    if not cause and outcome:
        cause = {
            "stalled": "gate_stalled",
            "budget": "plan_budget_exhausted",
        }.get(outcome, "gate_rejected")
    if not cause:
        cause = "requested_exit" if isinstance(exc, SystemExit) else "process_error"
    raw = str(exc).strip()
    # str(SystemExit(7)) is "7". Prefer an explicit detail, then the meaning map.
    if isinstance(exc, SystemExit) and isinstance(exc.code, int) and (not raw or raw == str(exc.code)):
        detail = EXIT_DETAILS.get(exc.code, f"exit {exc.code}")
    elif raw:
        detail = raw
    else:
        detail = {
            "max_turns_exhausted": "model turn budget exhausted",
            "usage_limit": "model usage limit reached",
            "session_stalled": "model session produced no publishable artifact",
            "gate_stalled": "plan gate stopped improving",
            "plan_budget_exhausted": "plan repair budget exhausted",
        }.get(cause, exc.__class__.__name__)
    return "failed", int(code), cause, detail


def _direct_run_id() -> str:
    return RUN_ID


def active(shot_folder: str | Path) -> RunLayout | None:
    configured = os.environ.get(ENV)
    if not configured:
        return None
    shot = Path(shot_folder).expanduser().resolve()
    root = Path(configured).expanduser().resolve()
    runs = shot / "runs"
    try:
        rel = root.relative_to(runs)
    except ValueError:
        return None
    if len(rel.parts) != 1:
        return None
    return RunLayout(shot=shot, run_id=_validate_run_id(rel.name), root=root)


def latest(shot_folder: str | Path) -> RunLayout | None:
    shot = Path(shot_folder).expanduser().resolve()
    pointer = shot / "runs" / "latest.json"
    try:
        rec = json.loads(pointer.read_text(encoding="utf-8"))
        rid = _validate_run_id(rec["run_id"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    root = shot / "runs" / rid
    return RunLayout(shot=shot, run_id=rid, root=root) if root.is_dir() else None


def select(shot_folder: str | Path, run_id: str | None = None) -> RunLayout | None:
    shot = Path(shot_folder).expanduser().resolve()
    if run_id:
        rid = _validate_run_id(run_id)
        root = shot / "runs" / rid
        return RunLayout(shot=shot, run_id=rid, root=root) if root.is_dir() else None
    return active(shot) or latest(shot)


def list_runs(shot_folder: str | Path) -> list[dict[str, Any]]:
    shot = Path(shot_folder).expanduser().resolve()
    out = []
    runs = shot / "runs"
    if not runs.is_dir():
        return out
    for root in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True):
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        try:
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {}
        out.append(
            {
                "run_id": root.name,
                "started_at": manifest.get("started_at"),
                "state": status.get("state", "unknown"),
                "exit_code": status.get("exit_code"),
                "manifest": str(root / "manifest.json"),
            }
        )
    return out


def logs_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).logs


def reports_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).reports


def renders_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).evidence / "renders"


def readable_renders_dir(shot_folder: str | Path) -> Path:
    """Return the selected run's renders; mixed shot-wide output is not supported."""
    layout = select(shot_folder)
    if not layout:
        raise FileNotFoundError(f"no structured run under {Path(shot_folder) / 'runs'}; run the shot first")
    return layout.evidence / "renders"


def checkpoints_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).checkpoints


def scratch_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).scratch


def deliverables_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).deliverables


def shot_state_dir(shot_folder: str | Path) -> Path:
    """Durable cross-run state, separate from authored inputs and generated output."""
    return Path(shot_folder) / "state"


def write_latest_projection(layout: RunLayout, *, state: str) -> None:
    """Project one run into ``runs/latest.json``, the reading order's first step."""

    _write_latest(layout, state=state)


def _write_latest(layout: RunLayout, *, state: str) -> None:
    _atomic_json(
        layout.shot / "runs" / "latest.json",
        {
            "schema": LATEST_SCHEMA,
            "run_id": layout.run_id,
            "path": layout.run_id,
            "state": state,
            "updated_at": _now(),
        },
    )
