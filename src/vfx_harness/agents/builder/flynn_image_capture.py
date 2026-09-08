"""Claim-bound cold image capture and native payment capabilities."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import flynn_agents_sdk as flynn

from vfx_harness.agents import image_inputs
from vfx_harness.agents.builder import candidate_script, flynn_image_checks, prior
from vfx_harness.agents.builder.attempt_guard import AttemptBoundBlenderSession, UnitAttemptGuard
from vfx_harness.agents.builder.models import _RESET
from vfx_harness.domain.image_debts import image_contract_debt_cards
from vfx_harness.domain.judgment_debt_models import render_mode_for_medium, unit_observation_medium
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_selection_transaction as durable
from vfx_harness.orchestration.builder_execution_fence import require_builder_execution_lease
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file


def _identity(prepared):
    return [{"script_path": item.executed.script_path, "sha256": item.executed.script_sha256,
             "dependencies": [{"kind": dep.kind, "path": dep.path, "sha256": dep.sha256}
                              for dep in item.executed.dependencies]} for item in prepared]


class UnitImageCapture:
    """One bounded unit's image instruments; never completion or replay authority.

    The caller supplies its exact accepted prior paths and current candidate ownership
    check, and registers ``guard`` with both tools. Runtime budgets count captures as
    external operations. Each capture renders at most one baseline and one candidate.
    """

    def __init__(self, *, shot, attempt_guard: UnitAttemptGuard, fence_lease, session,
                 prior_paths: list[Path], check_candidate: Callable[[], None]):
        self.shot = shot
        self.attempt = attempt_guard
        self.lease = fence_lease
        self.session = AttemptBoundBlenderSession(session, attempt_guard)
        self.check_candidate = check_candidate
        self.root = shot.folder.expanduser().absolute()
        if attempt_guard.folder.resolve() != self.root.resolve():
            raise ValueError("image capture requires the exact attempt's shot")
        if attempt_guard.unit.construction.route != "procedural":
            raise ValueError("native image capture currently requires procedural construction")
        self.mode = render_mode_for_medium(unit_observation_medium(attempt_guard.unit))
        self.frames = tuple(sorted({point.frame for point in attempt_guard.unit.evaluation.judges}))
        self.candidate = candidate_script.exact_candidate_script_path(self.root, attempt_guard)
        # Keep receipt-backed lists intact so their own current-publication checks run.
        self.prior_paths = prior_paths
        self.prepared = prior._prepare_artifact_replay_inputs(self.root, [
            (path.expanduser().absolute().relative_to(self.root).as_posix(), path) for path in prior_paths
        ])
        self.inputs = _identity(self.prepared)
        self.preamble = prior._preamble(shot)
        self.preamble_sha = digest(self.preamble.encode())
        self.parent_hash = digest(json.dumps({
            "scene_setup_sha256": self.preamble_sha, "inputs": self.inputs,
        }, sort_keys=True).encode())
        self.state = {
            "unit_id": attempt_guard.claim.unit_id, "unit_hash": attempt_guard.claim.unit_digest,
            "parent_chain_hash": self.parent_hash,
            "image_debts": [card.as_dict() for card in image_contract_debt_cards(attempt_guard.unit)],
            "image_artifacts": {}, "image_adversaries": {},
        }
        self.check_current()
        self.payment = flynn_image_checks.image_check_tool(
            attempt_guard=attempt_guard, fence_lease=fence_lease, comparison_state=self.state,
            check_candidate=self.require_current_images,
        )
        self.capture_tool = flynn.Tool.structured(
            "capture_unit_frame", description=(
                "Cold-render the harness-selected prior chain and current candidate at one declared judge frame. "
                "Returns both images and a current-candidate payment handle "
                "in the unit's declared medium at scale 0.5. "
                "A new candidate invalidates previous candidate handles. Does not accept the unit."
            ), parameters_json=json.dumps({
                "type": "object", "properties": {"frame": {"type": "integer", "enum": self.frames}},
                "required": ["frame"], "additionalProperties": False,
            }), validate=self.validate, execute=self.capture, external_action=True,
        )
        self.tools = (self.capture_tool, self.payment.tool)
        self.guard = flynn.DispatchGuard("current-vfx-unit-images", self.guard_dispatch)

    def check_current(self):
        require_builder_execution_lease(self.lease, self.root)
        self.attempt.check("native unit image capture")
        if self.attempt.claim.phase != "building":
            raise ValueError("native unit image capture requires a building claim")
        layout = run_artifacts.active(self.root)
        if layout is None or layout.run_id != self.attempt.claim.run_id:
            raise ValueError("native unit image capture requires the exact active run")
        if prior._preamble(self.shot) != self.preamble:
            raise ValueError("native image capture scene setup changed; start a new bound capture")
        if tuple(path.expanduser().absolute() for path in self.prior_paths) != tuple(
            item.source_path for item in self.prepared
        ):
            raise ValueError("native image capture prior order changed; start a new bound capture")
        if isinstance(self.prior_paths, prior._ReceiptBackedPriorPaths):
            self.prior_paths.require_current("native image capture")
        for item in self.prepared:
            prior.require_prepared_artifact_replay_input_unchanged(item)
        self.check_candidate()
        return layout

    def candidate_sha(self):
        return digest(read_real_file(self.root, self.candidate, "native image candidate"))

    def require_current_images(self):
        self.check_current()
        candidates = [r for r in self.state["image_artifacts"].values() if r["role"] == "live_candidate"]
        if candidates and any(r["candidate_sha256"] != self.candidate_sha() for r in candidates):
            raise ValueError("candidate changed since capture; capture the new candidate before proposing checks")

    async def guard_dispatch(self, context):
        self.check_current()
        if context.call.name == self.payment.tool.name:
            return await self.payment.guard.check(context)
        return flynn.GuardDecision(True, "current VFX image capture attempt, candidate and prior chain")

    def validate(self, arguments):
        if (set(arguments) != {"frame"} or type(arguments["frame"]) is not int
                or arguments["frame"] not in self.frames):
            raise ValueError(f"capture_unit_frame requires one declared judge frame: {self.frames}")

    def restore_prior(self):
        self.check_current()
        self.session.run(_RESET)
        self.session.run(self.preamble)
        prior._run_prior_paths(self.session, self.prior_paths, self.prepared)
        self.check_current()

    def render(self, frame, role, candidate_sha):
        self.session.run(f"bpy.context.scene.frame_set({frame})", journal=False)
        self.session.run(prior._ARTIFACT_EVALUATION_BARRIER, journal=False)
        rendered = self.session.call("render", frame=frame, mode=self.mode, scale=0.5)
        if rendered.get("frame") != frame or rendered.get("mode") != self.mode or rendered.get("diagnostic_only"):
            raise ValueError("image capture returned a different frame, medium or diagnostic render")
        payload = read_real_file(self.root, Path(rendered["image_path"]), "native rendered image")
        snapshot, _ = image_inputs.snapshot_image_payload(payload, "native unit render")
        record = {
            "run_id": self.attempt.claim.run_id, "unit_id": self.attempt.claim.unit_id,
            "unit_hash": self.attempt.claim.unit_digest, "claim_id": self.attempt.claim.claim_id,
            "parent_chain_hash": self.parent_hash, "candidate_sha256": candidate_sha,
            "frame": frame, "mode": self.mode, "scale": 0.5,
            "resolution": rendered["resolution"], "role": role, "sha256": digest(payload),
        }
        key = digest(json.dumps(record, sort_keys=True).encode())
        destination = run_artifacts.renders_dir(self.root) / f"{key}.png"
        if destination.exists() or destination.is_symlink():
            if read_real_file(self.root, destination, "existing image capture") != payload:
                raise ValueError("immutable image capture destination changed; preserve it and stop")
        else:
            durable.durable_replace_file_bytes(self.root, destination, payload)
        record.update(path=destination.relative_to(self.root).as_posix(), handle=f"image:{key}")
        return record, snapshot

    def reopen(self, record):
        payload = read_real_file(self.root, self.root / record["path"], "captured unit image")
        if digest(payload) != record["sha256"]:
            raise ValueError("captured image changed; preserve it and stop")
        return image_inputs.snapshot_image_payload(payload, record["path"])[0]

    async def capture(self, arguments):
        self.validate(arguments)
        layout = self.check_current()
        frame = arguments["frame"]
        with self.lease.operation(self.root):
            prepared_candidate = prior._prepare_artifact_replay_inputs(self.root, [
                (self.candidate.relative_to(self.root).as_posix(), self.candidate)
            ])[0]
            candidate_sha = prepared_candidate.executed.script_sha256
            adversary = self.state["image_adversaries"].get(frame)
            if adversary is None:
                self.restore_prior()
                adversary, _ = self.render(frame, "pre_unit_adversary", None)
            else:
                self.reopen(adversary)
            self.restore_prior()
            prior._run_artifact_script(self.session, self.candidate, prepared_candidate)
            candidate, _ = self.render(frame, "live_candidate", candidate_sha)
            if candidate["resolution"] != adversary["resolution"]:
                raise ValueError("candidate/adversary resolutions differ; preserve the canonical render settings")
            self.check_current()
            prior.require_prepared_artifact_replay_input_unchanged(prepared_candidate)
            baseline_image = self.reopen(adversary)
            candidate_image = self.reopen(candidate)
            report = {
                "schema": "vfx-harness.unit-image-capture/v1", "attempt": self.attempt.claim.as_dict(),
                "scene_setup_sha256": self.preamble_sha,
                "replay_inputs": self.inputs, "candidate_input": _identity((prepared_candidate,))[0],
                "adversary": adversary, "candidate": candidate, "accepted": False,
            }
            report_path = layout.write_report(candidate["handle"].replace(":", "-"), report)
            self.check_current()
            prior.require_prepared_artifact_replay_input_unchanged(prepared_candidate)
            self.reopen(adversary)
            self.reopen(candidate)
            report_bytes = read_real_file(self.root, report_path, "unit image capture report")
            if json.loads(report_bytes) != report:
                raise ValueError("unit image capture report changed before handle registration")
            # No partial capture is exposed. Keep only this generation's candidate handles.
            registry = {key: row for key, row in self.state["image_artifacts"].items()
                        if row["role"] != "live_candidate" or row["candidate_sha256"] == candidate_sha}
            registry[candidate["handle"]] = candidate
            registry[adversary["handle"]] = adversary
            self.state["image_artifacts"] = registry
            self.state["image_adversaries"][frame] = adversary
            return flynn.ToolResult(content=(
                flynn.TextContent("Pre-unit adversary (harness selected):"), flynn.ImageContent(baseline_image.url),
                flynn.TextContent("Current candidate:"), flynn.ImageContent(candidate_image.url),
            ), data_json=json.dumps({
                "schema": "vfx-harness.unit-image-observation/v2", "claim_id": self.attempt.claim.claim_id,
                # The guarded registry and source-verifiable report retain the full
                # ownership binding. Model feedback needs the payment handle and pixels,
                # not two additional copies of the current run/unit/prefix identity.
                **{name: {key: record[key] for key in (
                    "handle", "frame", "path", "sha256", "candidate_sha256", "mode", "resolution", "scale",
                )} for name, record in (("candidate", candidate), ("adversary", adversary))},
                "accepted": False,
                "report": report_path.relative_to(self.root).as_posix(), "report_sha256": digest(report_bytes),
            }, sort_keys=True))
