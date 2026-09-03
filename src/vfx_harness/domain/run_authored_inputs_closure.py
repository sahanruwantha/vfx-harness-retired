"""Authored intent and selected construction witnesses observed at interruption.

Split from :mod:`run_authority_source_closure` as its own family (HIR-0172 step 4): the
exact ``brief.md`` bytes, every admissible ``refs/`` still, and the registration plus crop
of every ``refobs-*`` witness a selected unit names. Pure domain state; discovery lives in
the interruption capturer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.run_authority_source_identity import AuthoritySourceIdentity
from vfx_harness.domain.run_authority_source_identity import (
    canonical_digest as _canonical_digest,
)
from vfx_harness.domain.run_authority_source_identity import (
    canonical_source_rows as _source_rows,
)
from vfx_harness.domain.run_authority_source_identity import (
    derived_family_state as _family_state,
)
from vfx_harness.domain.run_authority_source_identity import (
    exact_record as _exact_record,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_canonical_source_rows as _require_canonical_rows,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_digest as _digest,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_family_state as _require_family_state,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_source_kind as _require_kind,
)

AUTHORED_INPUTS_SOURCE_CLOSURE_SCHEMA = "vfx-harness.interruption-authored-inputs-source-closure/v1"
REFOBS_WITNESS_SOURCE_SCHEMA = "vfx-harness.interruption-refobs-witness-source/v1"
@dataclass(frozen=True, slots=True)
class RefobsWitnessSource:
    """One selected construction witness: its typed registration and exact crop bytes."""

    SCHEMA: ClassVar[str] = REFOBS_WITNESS_SOURCE_SCHEMA

    token: str
    registration: AuthoritySourceIdentity
    crop: AuthoritySourceIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.startswith("refobs-"):
            raise ValueError("RefobsWitnessSource.token must be a refobs-* witness id")
        _require_kind(self.registration, "refobs_registration", "RefobsWitnessSource.registration")
        _require_kind(self.crop, "refobs_crop", "RefobsWitnessSource.crop")
        stem = f"state/refobs/{self.token}"
        if self.registration.locator != f"{stem}.json" or self.crop.locator != f"{stem}.png":
            raise ValueError(
                "RefobsWitnessSource locators must be the token's registry pair under state/refobs/"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "token": self.token,
            "registration": self.registration.as_dict(),
            "crop": self.crop.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: object, where: str = "refobs witness source") -> RefobsWitnessSource:
        row = _exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset({"token", "registration", "crop"}),
        )
        return cls(
            str(row["token"]),
            AuthoritySourceIdentity.from_dict(row["registration"], f"{where}.registration"),
            AuthoritySourceIdentity.from_dict(row["crop"], f"{where}.crop"),
        )


@dataclass(frozen=True, slots=True)
class AuthoredInputsSourceClosure:
    """Exact authored intent and every selected construction witness (HIR-0172 step 4).

    ``brief`` is the exact ``brief.md`` bytes or explicit absence; ``references`` are every
    admissible ``refs/`` still (direct members, sorted by locator); ``refobs`` bind the
    registration and crop of every witness a selected unit names. A whole-frame reference
    or an unselected nearby crop cannot enter by proximity, and an empty mapping cannot
    stand in for the family.
    """

    SCHEMA: ClassVar[str] = AUTHORED_INPUTS_SOURCE_CLOSURE_SCHEMA

    state: str
    brief: AuthoritySourceIdentity | None
    references: tuple[AuthoritySourceIdentity, ...]
    refobs: tuple[RefobsWitnessSource, ...]

    def __post_init__(self) -> None:
        if self.brief is not None:
            _require_kind(self.brief, "brief", "AuthoredInputsSourceClosure.brief")
            if self.brief.locator != "brief.md":
                raise ValueError("AuthoredInputsSourceClosure.brief must be the shot-root brief.md")
        _require_canonical_rows(
            self.references,
            "AuthoredInputsSourceClosure.references",
            kind="reference_still",
        )
        for row in self.references:
            if not row.locator.startswith("refs/") or "/" in row.locator[len("refs/"):]:
                raise ValueError("AuthoredInputsSourceClosure.references must be direct refs/ members")
        if not isinstance(self.refobs, tuple) or any(
            not isinstance(row, RefobsWitnessSource) for row in self.refobs
        ):
            raise ValueError("AuthoredInputsSourceClosure.refobs must be typed witness sources")
        tokens = [row.token for row in self.refobs]
        if tokens != sorted(set(tokens)):
            raise ValueError("AuthoredInputsSourceClosure.refobs must be sorted and unique by token")
        sources = self.sources()
        if len({row.locator for row in sources}) != len(sources):
            raise ValueError("authored-inputs source closure contains duplicate locators")
        _require_family_state(self.state, _family_state(sources), "AuthoredInputsSourceClosure.state")

    def sources(self) -> tuple[AuthoritySourceIdentity, ...]:
        rows: list[AuthoritySourceIdentity] = []
        if self.brief is not None:
            rows.append(self.brief)
        rows.extend(self.references)
        for witness in self.refobs:
            rows.append(witness.registration)
            rows.append(witness.crop)
        return tuple(rows)

    @classmethod
    def absent(cls) -> AuthoredInputsSourceClosure:
        return cls("absent", None, (), ())

    @classmethod
    def mint(
        cls,
        *,
        brief: AuthoritySourceIdentity | None,
        references: Iterable[AuthoritySourceIdentity],
        refobs: Iterable[RefobsWitnessSource],
    ) -> AuthoredInputsSourceClosure:
        rows = _source_rows(references, "AuthoredInputsSourceClosure.references", kind="reference_still")
        witnesses = tuple(sorted(refobs, key=lambda row: row.token))
        sources = (
            *(() if brief is None else (brief,)),
            *rows,
            *(source for witness in witnesses for source in (witness.registration, witness.crop)),
        )
        return cls(_family_state(sources), brief, rows, witnesses)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "state": self.state,
            "brief": None if self.brief is None else self.brief.as_dict(),
            "references": [row.as_dict() for row in self.references],
            "refobs": [row.as_dict() for row in self.refobs],
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "closure_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "authored inputs source closure",
    ) -> AuthoredInputsSourceClosure:
        row = _exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset({"state", "brief", "references", "refobs", "closure_digest"}),
        )
        candidate = cls(
            str(row["state"]),
            None if row["brief"] is None else AuthoritySourceIdentity.from_dict(row["brief"], f"{where}.brief"),
            tuple(
                AuthoritySourceIdentity.from_dict(item, f"{where}.references[{index}]")
                for index, item in enumerate(row["references"])
            ),
            tuple(
                RefobsWitnessSource.from_dict(item, f"{where}.refobs[{index}]")
                for index, item in enumerate(row["refobs"])
            ),
        )
        if _digest(row["closure_digest"], f"{where}.closure_digest") != candidate.digest:
            raise ValueError(f"{where}.closure_digest is stale")
        return candidate



__all__ = [
    "AUTHORED_INPUTS_SOURCE_CLOSURE_SCHEMA",
    "REFOBS_WITNESS_SOURCE_SCHEMA",
    "AuthoredInputsSourceClosure",
    "RefobsWitnessSource",
]
