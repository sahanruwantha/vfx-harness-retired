"""Harness-selected generated construction recorded as one Flynn operation."""

from __future__ import annotations

import json

import flynn_agents_sdk as flynn

from vfx_harness.agents.builder import unit_construction
from vfx_harness.orchestration import generate_construction


class UnitConstruction:
    """Retain exact promoted asset and witness bindings for the native attempt."""

    def __init__(self, shot, attempt_guard):
        self.shot = shot
        self.attempt = attempt_guard
        self.binding = None
        self.tool = flynn.Tool(
            "prepare_construction", self.validate, self.prepare,
            observation=True, external_action=True,
            description="Prepare the exact plan-selected generated construction; no acceptance authority.",
        )

    @staticmethod
    def validate(arguments):
        if json.loads(arguments) != {}:
            raise ValueError("construction preparation takes an empty argument object")

    async def prepare(self, arguments):
        self.validate(arguments)
        self.attempt.check("prepare native generated construction")
        if self.binding is not None:
            raise ValueError("construction is already prepared for this native attempt")
        unit = self.attempt.unit
        if unit.construction.route != "generate":
            raise ValueError("native construction preparation requires the selected generate route")
        promoted = unit_construction.resolve_unit_construction(
            self.shot, self.attempt.layer_id, unit, self.attempt.claim.unit_digest, self.attempt,
        )
        binding = generate_construction.prepare_promoted_construction_reuse(
            self.shot.folder, self.attempt.layer_id, unit.id, self.attempt.claim.unit_digest,
        )
        if binding is None or promoted is None or (
            binding.promoted.glb_relpath, binding.promoted.sha256,
            binding.promoted.unit_digest, binding.promoted.view_count,
        ) != (promoted.glb_relpath, promoted.sha256, promoted.unit_digest, promoted.view_count):
            raise ValueError("generated construction changed after preparation; preserve it and stop")
        self.attempt.check("bind native generated construction")
        generate_construction.commit_promoted_construction_reuse(binding)
        self.binding = binding
        return json.dumps(self.context(), sort_keys=True)

    def check(self):
        self.attempt.check("check native generated construction")
        if self.binding is not None:
            generate_construction.commit_promoted_construction_reuse(self.binding)

    def context(self):
        if self.binding is None:
            return None
        self.check()
        promoted = self.binding.promoted
        return {
            "construction_sha256": promoted.sha256,
            "unit_digest": promoted.unit_digest,
            "view_count": promoted.view_count,
            "instruction": "Use bvfx_import_construction() with no arguments in the candidate; "
                           "assign imported objects only to the unit's declared semantic roles. "
                           "Never fetch or select another asset.",
            "acceptance_authorized": False,
        }
