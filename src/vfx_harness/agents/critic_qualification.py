"""Admission of selected, artifact-backed claims for a native critic invocation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.orchestration import critic_qualification_publication


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
    """An owning caller selects a measured set; the actual invocation selects one exact member."""

    def __init__(self, folder: Path, claims: tuple[Claim, ...], *, axes: tuple[str, ...], frames: tuple[int, ...]):
        self.semantics = selected_semantics(claims, axes=axes, frames=frames)
        self.folder = folder
        self.claims = claims
        self.snapshot = digest([asdict(claim) for claim in claims])
        self.references = []
        self.proofs = []
        for claim in claims:
            record = critic_qualification_publication.read_selected(folder, claim, f"critic claim {claim.id}")
            q = claim.qualification
            self.proofs.append(record)
            self.references.append({"claim_id": claim.id, "path": q["artifact"], "sha256": q["artifact_sha256"],
                                    "suite": q["suite"]})

    def check(self) -> None:
        if digest([asdict(claim) for claim in self.claims]) != self.snapshot:
            raise ValueError("selected critic claims changed; prepare a new qualification admission")
        for claim, expected in zip(self.claims, self.proofs, strict=True):
            record = critic_qualification_publication.read_selected(self.folder, claim, f"critic claim {claim.id}")
            if digest(record) != digest(expected):
                raise ValueError("critic qualification proof changed; reselect current authority")

    def admit(self, profile: dict) -> None:
        self.check()
        actual = digest(profile)
        for source, record in zip(self.references, self.proofs, strict=True):
            matches = [member for member in record["profiles"] if member["native_invocation_sha256"] == actual]
            if len(matches) != 1:
                raise ValueError(
                    f"critic qualification {source['claim_id']} native invocation mismatch: "
                    f"no exact measured member for {actual}; requalify the current configuration"
                )
            source["native_invocation_sha256"] = actual
