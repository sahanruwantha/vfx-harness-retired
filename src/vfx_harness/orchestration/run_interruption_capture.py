"""Fence-held capture of the live authority source graph into a run-owned archive.

The capturer resolves the closed source families of one shot while the caller holds
the shared shot-authority writer fence, classifies every source through the one domain
classification, copies its exact bytes into the target run's content-addressed archive,
and returns the typed snapshot.  Two captures bracket terminalization; their equality is
the authority observation an interruption receipt binds.  The evaluator later reopens
only the archive, never this live tree (HIR-0172).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain import construction
from vfx_harness.domain.authority_head_records import (
    AuthorityHeadRecordError,
    parse_jit_view_pointer,
    parse_plan_pointer,
)
from vfx_harness.domain.authority_state_commit_records import (
    AuthorityStateCoordinatorHead,
    AuthorityStatePendingPointer,
    AuthorityStateTransitionCommit,
)
from vfx_harness.domain.authority_state_record_primitives import (
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
)
from vfx_harness.domain.authority_state_transition_records import (
    AuthorityStateTransitionIntent,
    AuthorityStateTransitionProposal,
)
from vfx_harness.domain.brief import REFERENCE_STILL_SUFFIXES
from vfx_harness.domain.run_authority_source_closure import (
    AcceptedStateSourceClosure,
    AuthoredInputsSourceClosure,
    DurableStateSourceClosure,
    InterruptionAuthoritySourceClosure,
    RefobsWitnessSource,
    SelectedPlanSourceClosure,
)
from vfx_harness.domain.run_authority_source_identity import AuthoritySourceIdentity
from vfx_harness.domain.run_authority_source_records import (
    SEMANTIC_RECORD_CLASSES,
    classify_authority_source,
    decode_strict_json_object,
)
from vfx_harness.domain.run_interruption_archive import (
    ArchivedSourceObject,
    InterruptionArchiveManifest,
)
from vfx_harness.domain.run_interruption_records import (
    InterruptionAuthorityObservation,
    InterruptionTranscriptFrontier,
    RunAuthoritySnapshot,
    derive_transcript_frontier,
)
from vfx_harness.domain.shot_ledger_v2 import ShotLedgerV2
from vfx_harness.infrastructure.trusted_files import TrustedFileError
from vfx_harness.observability.run_interruption_archive import (
    RunInterruptionArchiveError,
    read_regular_file,
    store_archive_object,
)
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.shot_authority_capture import (
    ShotAuthorityWriterCapability,
    require_live_shot_authority_writer,
)

PLAN_POINTER_LOCATOR = "plans/current.json"
PLAN_AMENDMENTS_LOCATOR = "plan_amendments.jsonl"
PLAN_RESOLUTIONS_LOCATOR = "state/plan-resolutions.jsonl"
EFFECTIVE_VIEW_POINTER_LOCATOR = "state/jit-layers/current.json"
ACCEPTED_LEDGER_LOCATOR = "shot.json"
JUDGMENT_DEBTS_LOCATOR = "state/judgment-debts.jsonl"
JUDGMENT_PAYMENT_ATTEMPTS_LOCATOR = "state/judgment-payment-attempts.jsonl"
DURABLE_STATE_POINTER_LOCATOR = "state/authority-state/current.json"
DURABLE_STATE_PENDING_LOCATOR = "state/authority-state/pending.json"
DURABLE_STATE_OBJECTS_DIRECTORY = "state/authority-state/objects"
WORK_UNIT_STATE_DIRECTORY = "state/work-units"
TRANSCRIPT_DIRECTORY = "logs/transcripts"


class RunInterruptionCaptureError(ValueError):
    """The live source graph could not be captured exactly and completely."""


@dataclass(frozen=True, slots=True)
class RunAuthorityCapture:
    """One authority snapshot and the archive rows its sources were copied into."""

    snapshot: RunAuthoritySnapshot
    objects: tuple[ArchivedSourceObject, ...]


@dataclass(frozen=True, slots=True)
class TranscriptFrontierCapture:
    frontiers: tuple[InterruptionTranscriptFrontier, ...]
    objects: tuple[ArchivedSourceObject, ...]


@dataclass(frozen=True, slots=True)
class InterruptionObservationCapture:
    """Before/after authority observation, transcript frontiers, and the archive manifest."""

    observation: InterruptionAuthorityObservation
    frontiers: tuple[InterruptionTranscriptFrontier, ...]
    archive: InterruptionArchiveManifest


class _Capturer:
    def __init__(self, shot_root: Path, run_root: Path) -> None:
        self.shot_root = shot_root
        self.run_root = run_root
        self.objects: list[ArchivedSourceObject] = []
        self._identities: dict[str, AuthoritySourceIdentity] = {}

    def source(self, source_kind: str, locator: str) -> AuthoritySourceIdentity | None:
        """Classify and archive one shot-relative source, or report explicit absence."""

        cached = self._identities.get(locator)
        if cached is not None:
            if cached.source_kind != source_kind:
                raise RunInterruptionCaptureError(
                    f"authority source {locator!r} was observed under two kinds: "
                    f"{cached.source_kind}, {source_kind}"
                )
            return cached
        path = self.shot_root / locator
        if path.is_symlink():
            raise RunInterruptionCaptureError(f"authority source {locator!r} is a symlink; capture refuses aliases")
        if not path.exists():
            return None
        try:
            snapshot = plan_bundle_integrity.read_real_file_snapshot(
                self.shot_root,
                path,
                f"authority source {locator}",
            )
        except (OSError, TrustedFileError, ValueError, plan_bundle_integrity.PlanPublicationError) as exc:
            raise RunInterruptionCaptureError(f"authority source {locator!r} is not readable exactly: {exc}") from exc
        identity = classify_authority_source(source_kind, locator, snapshot.payload)
        try:
            self.objects.append(
                store_archive_object(self.run_root, namespace="shot", locator=locator, payload=snapshot.payload)
            )
        except RunInterruptionArchiveError as exc:
            raise RunInterruptionCaptureError(str(exc)) from exc
        self._identities[locator] = identity
        return identity

    def payload(self, locator: str) -> bytes:
        """Return the exact bytes of an already captured source."""

        return plan_bundle_integrity.read_real_file_snapshot(
            self.shot_root,
            self.shot_root / locator,
            f"authority source {locator}",
        ).payload

    # -- authored inputs family ----------------------------------------------------

    def authored_inputs(self, selected_plan: SelectedPlanSourceClosure) -> AuthoredInputsSourceClosure:
        """Exact brief, admissible reference stills, and every selected refobs witness.

        Admissible stills are the regular files directly under ``refs/`` with an image
        suffix, the same rule the shot loader applies; a symlink or non-regular entry is a
        hostile namespace and refuses capture. Witness tokens come only from the units of
        the captured effective view, so an unselected crop cannot enter by proximity.
        """

        brief = self.source("brief", "brief.md")
        references: list[AuthoritySourceIdentity] = []
        refs_root = self.shot_root / "refs"
        if refs_root.is_symlink():
            raise RunInterruptionCaptureError("refs/ is a symlink; capture refuses aliases")
        if refs_root.is_dir():
            for entry in sorted(refs_root.iterdir(), key=lambda item: item.name):
                if entry.is_symlink():
                    raise RunInterruptionCaptureError(
                        f"refs/{entry.name} is a symlink; capture refuses aliases"
                    )
                if entry.suffix.lower() not in REFERENCE_STILL_SUFFIXES:
                    continue
                if not entry.is_file():
                    raise RunInterruptionCaptureError(
                        f"refs/{entry.name} is not a regular file; the refs/ namespace is hostile"
                    )
                identity = self.source("reference_still", f"refs/{entry.name}")
                if identity is not None:
                    references.append(identity)
        witnesses: list[RefobsWitnessSource] = []
        for token in self._selected_witness_tokens(selected_plan):
            registration = self.source("refobs_registration", f"state/refobs/{token}.json")
            crop = self.source("refobs_crop", f"state/refobs/{token}.png")
            if registration is None or crop is None:
                raise RunInterruptionCaptureError(
                    f"selected construction witness {token} has no complete registry pair "
                    "under state/refobs/"
                )
            witnesses.append(RefobsWitnessSource(token, registration, crop))
        return AuthoredInputsSourceClosure.mint(brief=brief, references=references, refobs=witnesses)

    def _selected_witness_tokens(self, selected_plan: SelectedPlanSourceClosure) -> tuple[str, ...]:
        """Every refobs-* witness a unit of the captured effective view binds."""

        layers_member = next(
            (
                member
                for member in selected_plan.effective_view_members
                if member.locator.endswith("/layers.json")
            ),
            None,
        )
        if layers_member is None or layers_member.source_state == "raw_invalid":
            return ()
        document, _reason = decode_strict_json_object(self.payload(layers_member.locator))
        if document is None:
            return ()
        try:
            return construction.selected_witness_tokens(document)
        except ValueError as exc:
            raise RunInterruptionCaptureError(str(exc)) from exc

    # -- selected plan family ------------------------------------------------------

    def selected_plan(self) -> SelectedPlanSourceClosure:
        amendments = self.source("plan_amendments", PLAN_AMENDMENTS_LOCATOR)
        resolutions = self.source("plan_resolutions", PLAN_RESOLUTIONS_LOCATOR)
        pointer = self.source("plan_pointer", PLAN_POINTER_LOCATOR)
        if pointer is None:
            return SelectedPlanSourceClosure.absent(plan_amendments=amendments, plan_resolutions=resolutions)
        manifest = None
        members: list[AuthoritySourceIdentity] = []
        view_pointer = None
        view_members: list[AuthoritySourceIdentity] = []
        if pointer.source_state == "record_valid":
            projection = parse_plan_pointer(decode_strict_json_object(self.payload(PLAN_POINTER_LOCATOR))[0])
            manifest_locator = f"{projection.bundle}/bundle.json"
            manifest = self.source("plan_bundle_manifest", manifest_locator)
            if manifest is None:
                raise RunInterruptionCaptureError(
                    f"selected plan pointer names a bundle whose manifest is absent: {manifest_locator}"
                )
            if manifest.source_state == "record_valid":
                document, _reason = decode_strict_json_object(self.payload(manifest_locator))
                assert document is not None
                artifacts = document.get("artifacts")
                if not isinstance(artifacts, dict) or not artifacts:
                    raise RunInterruptionCaptureError("selected bundle manifest declares no artifacts")
                for name in sorted(artifacts):
                    member = self.source("plan_bundle_member", f"{projection.bundle}/{name}")
                    if member is None:
                        raise RunInterruptionCaptureError(f"selected bundle member is absent: {name}")
                    members.append(member)
                view_pointer = self.source("effective_view_pointer", EFFECTIVE_VIEW_POINTER_LOCATOR)
                if view_pointer is not None and view_pointer.source_state == "record_valid":
                    view_document, _reason = decode_strict_json_object(self.payload(EFFECTIVE_VIEW_POINTER_LOCATOR))
                    view = parse_jit_view_pointer(view_document)
                    for name in sorted(view.artifacts):
                        member_locator = view.artifacts[name]
                        member = self.source("effective_view_member", member_locator)
                        if member is None:
                            raise RunInterruptionCaptureError(f"effective view member is absent: {member_locator}")
                        view_members.append(member)
        return SelectedPlanSourceClosure.mint_present(
            plan_pointer=pointer,
            bundle_manifest=manifest,
            bundle_members=members,
            effective_view_pointer=view_pointer,
            effective_view_members=view_members,
            plan_amendments=amendments,
            plan_resolutions=resolutions,
        )

    # -- accepted state family -----------------------------------------------------

    def accepted_state(self) -> AcceptedStateSourceClosure:
        debts = self.source("judgment_debts", JUDGMENT_DEBTS_LOCATOR)
        attempts = self.source("judgment_payment_attempts", JUDGMENT_PAYMENT_ATTEMPTS_LOCATOR)
        ledger = self.source("accepted_ledger", ACCEPTED_LEDGER_LOCATOR)
        if ledger is None:
            return AcceptedStateSourceClosure.absent(judgment_debts=debts, judgment_payment_attempts=attempts)
        document, reason = decode_strict_json_object(self.payload(ACCEPTED_LEDGER_LOCATOR))
        members: list[AuthoritySourceIdentity] = []
        if document is None:
            ledger = AuthoritySourceIdentity.raw_invalid(
                source_kind="accepted_ledger",
                locator=ACCEPTED_LEDGER_LOCATOR,
                byte_count=ledger.byte_count,
                sha256=ledger.sha256,
                invalid_reason=reason or "malformed_json",
            )
        else:
            stored = document.get("accepted_build")
            if stored is not None:
                try:
                    index = ShotLedgerV2.from_dict(stored, "shot.json.accepted_build")
                except ValueError as exc:
                    raise RunInterruptionCaptureError(f"stored accepted-build index is malformed: {exc}") from exc
                locators: list[str] = []
                for row in index.accepted_layers:
                    locators.extend((row.composed_script_locator, row.sealed_outcome_locator))
                if index.acceptance is not None:
                    for moment in index.acceptance.moment_evidence:
                        locators.extend(
                            (moment.evidence_record_locator, moment.render_locator, moment.reference_locator)
                        )
                for locator in sorted(set(locators)):
                    member = self.source("accepted_member", locator)
                    if member is None:
                        raise RunInterruptionCaptureError(f"accepted-build member is absent: {locator}")
                    members.append(member)
        return AcceptedStateSourceClosure.mint_present(
            ledger=ledger,
            members=members,
            judgment_debts=debts,
            judgment_payment_attempts=attempts,
        )

    # -- durable state family ------------------------------------------------------

    def _object_locator(self, reference: AuthorityStateRecordRef) -> str:
        return reference.locator

    def _walk_records(
        self,
        start: list[AuthorityStateRecordRef],
        records: dict[str, AuthoritySourceIdentity],
        *,
        exclude: str | None,
    ) -> None:
        queue = list(start)
        seen: set[str] = set()
        while queue:
            reference = queue.pop(0)
            locator = self._object_locator(reference)
            if locator in seen:
                continue
            seen.add(locator)
            identity = self.source("durable_state_record", locator)
            if identity is None:
                raise RunInterruptionCaptureError(f"authority-state record is absent: {locator}")
            if locator != exclude:
                records[locator] = identity
            if identity.source_state != "record_valid":
                continue
            record_class = SEMANTIC_RECORD_CLASSES.get(identity.record_schema or "")
            if record_class is None:
                continue
            document, _reason = decode_strict_json_object(self.payload(locator))
            assert document is not None
            try:
                record = record_class.parse(document, "authority-state record")
            except AuthorityStateRecordError as exc:  # pragma: no cover - classified valid above
                raise RunInterruptionCaptureError(str(exc)) from exc
            if isinstance(record, AuthorityStateCoordinatorHead):
                queue.extend((record.commit_ref, record.evaluation_ref))
            elif isinstance(record, AuthorityStateTransitionCommit):
                queue.append(record.intent_ref)
            elif isinstance(record, AuthorityStateTransitionIntent):
                queue.append(record.proposal_ref)
            elif isinstance(record, AuthorityStateTransitionProposal):
                queue.append(record.capsule_set_ref)

    def durable_state(self) -> DurableStateSourceClosure:
        records: dict[str, AuthoritySourceIdentity] = {}
        members: list[AuthoritySourceIdentity] = []
        directory = self.shot_root / WORK_UNIT_STATE_DIRECTORY
        if directory.is_symlink():
            raise RunInterruptionCaptureError("work-unit state directory is a symlink")
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                if path.name.startswith(".") or not path.name.endswith(".json"):
                    continue
                member = self.source("durable_state_member", f"{WORK_UNIT_STATE_DIRECTORY}/{path.name}")
                if member is not None:
                    members.append(member)
        pending = self.source("durable_state_pending", DURABLE_STATE_PENDING_LOCATOR)
        if pending is not None and pending.source_state == "record_valid":
            document, _reason = decode_strict_json_object(self.payload(DURABLE_STATE_PENDING_LOCATOR))
            pointer = AuthorityStatePendingPointer.parse(document, "authority-state pending pointer")
            self._walk_records([pointer.intent_ref], records, exclude=None)
        current = self.source("durable_state_pointer", DURABLE_STATE_POINTER_LOCATOR)
        if current is None:
            return DurableStateSourceClosure.absent(
                pending=pending,
                records=records.values(),
                members=members,
            )
        head = None
        if current.source_state == "record_valid":
            head_locator = f"{DURABLE_STATE_OBJECTS_DIRECTORY}/{current.sha256}/record.json"
            head = self.source("durable_state_record", head_locator)
            if head is None:
                raise RunInterruptionCaptureError(
                    "selected authority-state head has no immutable object copy: " + head_locator
                )
            if head.source_state == "record_valid":
                document, _reason = decode_strict_json_object(self.payload(head_locator))
                head_record = AuthorityStateCoordinatorHead.parse(document, "authority-state head")
                self._walk_records(
                    [head_record.commit_ref, head_record.evaluation_ref],
                    records,
                    exclude=head_locator,
                )
            records.pop(head_locator, None)
        return DurableStateSourceClosure.mint_present(
            current_pointer=current,
            head=head,
            records=records.values(),
            members=members,
            pending=pending,
        )


def capture_run_authority(
    shot_root: str | Path,
    run_root: str | Path,
    *,
    run_id: str,
    writer_capability: ShotAuthorityWriterCapability,
    captured_at: str,
) -> RunAuthorityCapture:
    """Capture the complete closed source graph of one shot under the shared fence."""

    shot = Path(shot_root).expanduser().absolute()
    run = Path(run_root).expanduser().absolute()
    require_live_shot_authority_writer(writer_capability, shot)
    capturer = _Capturer(shot, run)
    try:
        selected_plan = capturer.selected_plan()
        accepted_state = capturer.accepted_state()
        durable_state = capturer.durable_state()
        authored_inputs = capturer.authored_inputs(selected_plan)
        closure = InterruptionAuthoritySourceClosure(
            selected_plan,
            accepted_state,
            durable_state,
            authored_inputs,
        )
    except (AuthorityHeadRecordError, AuthorityStateRecordError, ValueError) as exc:
        if isinstance(exc, RunInterruptionCaptureError):
            raise
        raise RunInterruptionCaptureError(f"authority source graph cannot be represented: {exc}") from exc
    snapshot = RunAuthoritySnapshot.mint(run_id=run_id, source_closure=closure, captured_at=captured_at)
    return RunAuthorityCapture(snapshot=snapshot, objects=tuple(capturer.objects))


def capture_transcript_frontiers(
    run_root: str | Path,
    *,
    run_id: str,
    captured_at: str,
) -> TranscriptFrontierCapture:
    """Snapshot every existing transcript of the run and archive its exact bytes."""

    run = Path(run_root).expanduser().absolute()
    directory = run / TRANSCRIPT_DIRECTORY
    frontiers: list[InterruptionTranscriptFrontier] = []
    objects: list[ArchivedSourceObject] = []
    if directory.is_symlink():
        raise RunInterruptionCaptureError("transcript directory is a symlink")
    if directory.is_dir():
        for path in sorted(directory.rglob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                raise RunInterruptionCaptureError(f"transcript is not a regular file: {path}")
            locator = path.relative_to(run).as_posix()
            payload = read_regular_file(path, where=f"transcript {locator}")
            frontiers.append(
                derive_transcript_frontier(run_id=run_id, locator=locator, payload=payload, captured_at=captured_at)
            )
            try:
                objects.append(store_archive_object(run, namespace="run", locator=locator, payload=payload))
            except RunInterruptionArchiveError as exc:
                raise RunInterruptionCaptureError(str(exc)) from exc
    frontiers.sort(key=lambda row: row.locator)
    return TranscriptFrontierCapture(frontiers=tuple(frontiers), objects=tuple(objects))


def capture_interruption_observation(
    shot_root: str | Path,
    run_root: str | Path,
    *,
    run_id: str,
    writer_capability: ShotAuthorityWriterCapability,
    clock: Callable[[], str],
) -> InterruptionObservationCapture:
    """Bracket terminal capture with two authority snapshots and archive everything.

    The before snapshot, the transcript frontiers, and the after snapshot are taken in
    that order with timestamps from ``clock``; an authority change between the snapshots
    makes the observation unrepresentable and raises.
    """

    before_at = clock()
    before = capture_run_authority(
        shot_root,
        run_root,
        run_id=run_id,
        writer_capability=writer_capability,
        captured_at=before_at,
    )
    transcripts = capture_transcript_frontiers(run_root, run_id=run_id, captured_at=clock())
    after = capture_run_authority(
        shot_root,
        run_root,
        run_id=run_id,
        writer_capability=writer_capability,
        captured_at=clock(),
    )
    try:
        observation = InterruptionAuthorityObservation(
            run_id=run_id,
            before=before.snapshot,
            after=after.snapshot,
        )
    except ValueError as exc:
        raise RunInterruptionCaptureError(str(exc)) from exc
    archive = InterruptionArchiveManifest.mint(
        run_id=run_id,
        captured_at=before_at,
        authority_digest=observation.before.authority_digest,
        objects=(*before.objects, *transcripts.objects, *after.objects),
    )
    return InterruptionObservationCapture(
        observation=observation,
        frontiers=transcripts.frontiers,
        archive=archive,
    )


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "InterruptionObservationCapture",
    "RunAuthorityCapture",
    "RunInterruptionCaptureError",
    "TranscriptFrontierCapture",
    "capture_interruption_observation",
    "capture_run_authority",
    "capture_transcript_frontiers",
]
