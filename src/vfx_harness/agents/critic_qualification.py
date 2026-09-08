"""Admission of selected, artifact-backed claims for a native critic invocation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from vfx_harness.domain.work_units import claims as claim_contracts
from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.orchestration import critic_qualification_publication
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def selected_semantics(claims: tuple[Claim, ...], *, axes: tuple[str, ...], frames: tuple[int, ...]) -> list[dict]:
    """Share exact claim inputs between calibration and admission, without inventing authority."""
    if not claims or any(not isinstance(claim, Claim) for claim in claims):
        raise ValueError(
            "native qualification requires parsed selected claims; implicit layer/debt claims need authority"
        )
    if len({claim.id for claim in claims}) != len(claims):
        raise ValueError("native qualification requires distinct selected claim ids")
    for claim in claims:
        if claim.authority != "qualified_qualitative_required" or not claim.required:
            raise ValueError(f"critic claim {claim.id} requires a required qualitative claim")
        if claim.axis not in axes or not set(claim.moments).intersection(frames):
            raise ValueError(f"critic claim {claim.id} is outside invocation scope; select its owning judge point")
    return [{key: value for key, value in asdict(claim).items() if key != "qualification"} for claim in claims]


class Admission:
    """An owning caller selects claims; model output can neither select nor qualify them."""

    def __init__(
        self,
        folder: Path,
        claims: tuple[Claim, ...],
        *,
        model: str,
        prompt: str,
        image_shape: str,
        axes: tuple[str, ...],
        frames: tuple[int, ...],
    ):
        self.semantics = selected_semantics(claims, axes=axes, frames=frames)
        self.folder = folder
        self.claims = claims
        self.snapshot = digest([asdict(claim) for claim in claims])
        self.references = []
        self.proofs = []
        for claim in claims:
            if claim.authority != "qualified_qualitative_required" or not claim.qualification:
                raise ValueError(f"critic claim {claim.id} requires explicit qualitative qualification authority")
            q = claim.qualification
            payload = read_real_file(folder, folder / q["artifact"], "critic qualification")
            claim_contracts.validate_qualification(folder, claim, f"critic claim {claim.id}")
            if hashlib.sha256(payload).hexdigest() != q["artifact_sha256"]:
                raise ValueError(f"critic qualification {claim.id} changed; reselect current authority")
            expected = {
                "judge_model": model,
                "prompt": hashlib.sha256(prompt.encode()).hexdigest(),
                "evidence_shape": image_shape,
            }
            for key, value in expected.items():
                if q[key] != value:
                    raise ValueError(
                        f"critic qualification {claim.id} {key} differs from invocation; requalify this judge"
                    )
            if not any(binding.kind == "qualification" and binding.id == q["suite"] for binding in claim.evidence):
                raise ValueError(f"critic claim {claim.id} must bind its exact qualification suite {q['suite']}")
            record = json.loads(payload)
            if type(record.get("schema")) is not int or record["schema"] != 1:
                raise ValueError(f"critic qualification {claim.id} requires integer artifact schema 1")
            invocation = record.get("native_invocation_sha256")
            if (
                not isinstance(invocation, str)
                or len(invocation) != 64
                or any(c not in "0123456789abcdef" for c in invocation)
            ):
                raise ValueError(
                    f"critic qualification {claim.id} requires native_invocation_sha256; requalify this judge"
                )
            if record.get("claim_id") != claim.id:
                raise ValueError(f"critic qualification must measure selected claim {claim.id}")
            critic_qualification_publication.verify(folder, record)
            self.proofs.append(record)
            self.references.append(
                {
                    "claim_id": claim.id,
                    "path": q["artifact"],
                    "sha256": q["artifact_sha256"],
                    "suite": q["suite"],
                    "native_invocation_sha256": invocation,
                }
            )

    def check(self) -> None:
        if digest([asdict(claim) for claim in self.claims]) != self.snapshot:
            raise ValueError("selected critic claims changed; prepare a new qualification admission")
        for source in self.references:
            payload = read_real_file(self.folder, self.folder / source["path"], "critic qualification")
            if hashlib.sha256(payload).hexdigest() != source["sha256"]:
                raise ValueError(f"critic qualification {source['claim_id']} bytes changed; reselect current authority")
        for record in self.proofs:
            critic_qualification_publication.verify(self.folder, record)

    def admit(self, profile: dict) -> None:
        self.check()
        actual = digest(profile)
        for source in self.references:
            if source["native_invocation_sha256"] != actual:
                raise ValueError(
                    f"critic qualification {source['claim_id']} native invocation mismatch: expected "
                    f"{source['native_invocation_sha256']}, got {actual}; requalify the current configuration"
                )
