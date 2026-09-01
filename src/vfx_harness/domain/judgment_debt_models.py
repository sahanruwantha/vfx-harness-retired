"""Pure authority, activation, and lifecycle contracts for qualitative debt.

Provisional image judgment is durable debt.  It becomes due only at the earliest
dependency-complete replay prefix carrying every typed subject, never because an
unrelated mesh happens to exist.  This module is filesystem- and runtime-free so the
same provider-prefix compiler can schedule other deferred evidence kinds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from vfx_harness.domain.semantic_roles import match_semantic, selector_token_error

PROVISIONAL_STRENGTHS = frozenset({"approved_start", "planner_start"})
RENDERED_CARRIER_FAMILIES = frozenset({"mesh", "volume", "compositor"})
OBSERVATION_MEDIA = frozenset({"workbench_solid", "eevee"})
JUDGMENT_DEBT_LIFECYCLES = frozenset({"persistent", "layer", "window"})
JUDGMENT_DEBT_STATUSES = frozenset({"pending_not_due", "due", "satisfied", "falsified"})
TERMINAL_JUDGMENT_DEBT_STATUSES = frozenset({"satisfied", "falsified"})

# A judgment debt is one independently payable visual proposition.  Do not accept
# arbitrary claim labels here: the classifier is authority, while prose stays in
# ``statement``.  Extending either vocabulary is a schema/authority decision, not a
# convenient per-shot escape hatch.
JUDGMENT_DEBT_CLAIM_KINDS = frozenset({"atomic"})
JUDGMENT_DEBT_PROPERTIES = frozenset({"camera_framing", "reference_identity", "subject_appearance"})


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed string")
    return value


def _require_digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _require_canonical_digest(
    found: Any,
    expected: str,
    where: str,
    field: str,
) -> None:
    _require_digest(found, f"{where}.{field}")
    if found != expected:
        raise ValueError(f"{where}.{field} is stale; expected {expected!r}, found {found!r}")


def _require_text_tuple(value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (not value and not allow_empty):
        qualifier = "" if allow_empty else " non-empty"
        raise ValueError(f"{where} must be a{qualifier} tuple")
    for index, item in enumerate(value):
        _require_text(item, f"{where}[{index}]")
    if len(value) != len(set(value)):
        raise ValueError(f"{where} contains duplicates")
    return value


def _require_roles(value: Any, where: str) -> tuple[str, ...]:
    roles = _require_text_tuple(value, where)
    for role in roles:
        error = selector_token_error(role, where)
        if error:
            raise ValueError(error)
    return roles


def _input_text_sequence(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{where} must be a sequence of strings")
    return _require_text_tuple(tuple(value), where)


def _record(
    value: Any,
    where: str,
    schema: str | None,
    fields: tuple[str, ...],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    expected = {*fields, *({"schema"} if schema else set())}
    found = set(value)
    if found != expected:
        missing = sorted(expected - found, key=str)
        unexpected = sorted(found - expected, key=str)
        raise ValueError(f"{where} fields mismatch; missing={missing}; unexpected={unexpected}")
    if schema is not None and value["schema"] != schema:
        raise ValueError(f"{where}.schema must be {schema!r}, found {value['schema']!r}")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    return value


def _text_list(value: Any, where: str) -> tuple[str, ...]:
    return _require_text_tuple(tuple(_list(value, where)), where)


def _subject_bindings_from_dict(value: Any, where: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    bindings: list[tuple[str, tuple[str, ...]]] = []
    for index, raw in enumerate(_list(value, where)):
        row_where = f"{where}[{index}]"
        row = _record(raw, row_where, None, ("subject", "provider_ids"))
        bindings.append(
            (
                _require_text(row["subject"], f"{row_where}.subject"),
                _text_list(row["provider_ids"], f"{row_where}.provider_ids"),
            )
        )
    return tuple(bindings)


def _digest_pairs_from_dict(value: Any, where: str, id_key: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for index, raw in enumerate(_list(value, where)):
        row_where = f"{where}[{index}]"
        row = _record(raw, row_where, None, (id_key, "digest"))
        pairs.append(
            (_require_text(row[id_key], f"{row_where}.{id_key}"), _require_digest(row["digest"], f"{row_where}.digest"))
        )
    return tuple(pairs)


def _validate_digest_pairs(value: Any, where: str, *, allow_empty: bool = False) -> set[str]:
    if not isinstance(value, tuple) or (not value and not allow_empty):
        raise ValueError(f"{where} must be a{'n non-empty' if not allow_empty else ''} tuple")
    ids: list[str] = []
    for index, pair in enumerate(value):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError(f"{where}[{index}] must be an (id, digest) tuple")
        row_id, digest = pair
        ids.append(_require_text(row_id, f"{where}[{index}].id"))
        _require_digest(digest, f"{where}[{index}].digest")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{where} contains duplicate ids")
    return set(ids)


def _binding_references(value: Any, where: str) -> tuple[set[str], set[str]]:
    if not isinstance(value, tuple) or not value:
        raise ValueError(f"{where} must be a non-empty tuple")
    subjects: list[str] = []
    referenced: set[str] = set()
    for index, pair in enumerate(value):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError(f"{where}[{index}] must be a (subject, provider_ids) tuple")
        subject, provider_ids = pair
        subjects.append(_require_text(subject, f"{where}[{index}].subject"))
        referenced.update(_require_text_tuple(provider_ids, f"{where}[{index}].provider_ids"))
    if len(subjects) != len(set(subjects)):
        raise ValueError(f"{where} contains duplicate subjects")
    return set(subjects), referenced


def _subject_matches_provider(subject: str, provider_role: str) -> bool:
    """Overlap under the repository's literal-descendant/wildcard matcher."""
    return match_semantic(provider_role, (subject,)) or match_semantic(subject, (provider_role,))


@dataclass(frozen=True, slots=True)
class JudgmentPoint:
    frame: int
    ref: str

    def __post_init__(self) -> None:
        if isinstance(self.frame, bool) or not isinstance(self.frame, int) or self.frame < 1:
            raise ValueError("JudgmentPoint.frame must be a positive integer")
        _require_text(self.ref, "JudgmentPoint.ref")

    def as_dict(self) -> dict[str, Any]:
        return {"frame": self.frame, "ref": self.ref}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentPoint:
        row = _record(value, where, None, ("frame", "ref"))
        return cls(frame=row["frame"], ref=row["ref"])


@dataclass(frozen=True, slots=True)
class JudgmentDebtSeed:
    """One semantic proposition before its provider prefix is compiled."""

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-debt-seed/v1"

    requirement_id: str
    statement: str
    decision_strength: str
    claim_kind: str
    property: str
    owner_layer: str
    fault_owner: str
    subject_roles: tuple[str, ...]
    axes: tuple[str, ...]
    judge_points: tuple[JudgmentPoint, ...]
    observation_medium: str
    lifecycle: str
    bundle_digest: str
    carrier_families: tuple[str, ...]
    debt_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("requirement_id", "statement", "claim_kind", "property", "owner_layer", "fault_owner"):
            _require_text(getattr(self, name), f"JudgmentDebtSeed.{name}")
        if self.decision_strength not in PROVISIONAL_STRENGTHS:
            raise ValueError("JudgmentDebtSeed.decision_strength must be approved_start or planner_start")
        if self.claim_kind not in JUDGMENT_DEBT_CLAIM_KINDS:
            raise ValueError(
                "JudgmentDebtSeed.claim_kind must be one of " + ", ".join(sorted(JUDGMENT_DEBT_CLAIM_KINDS))
            )
        if self.property not in JUDGMENT_DEBT_PROPERTIES:
            raise ValueError("JudgmentDebtSeed.property must be one of " + ", ".join(sorted(JUDGMENT_DEBT_PROPERTIES)))
        _require_roles(self.subject_roles, "JudgmentDebtSeed.subject_roles")
        _require_text_tuple(self.axes, "JudgmentDebtSeed.axes")
        if (
            not isinstance(self.judge_points, tuple)
            or not self.judge_points
            or any(not isinstance(point, JudgmentPoint) for point in self.judge_points)
        ):
            raise ValueError("JudgmentDebtSeed.judge_points must be a non-empty tuple of JudgmentPoint values")
        points = [(point.frame, point.ref) for point in self.judge_points]
        if len(points) != len(set(points)):
            raise ValueError("JudgmentDebtSeed.judge_points contains duplicates")
        if self.observation_medium not in OBSERVATION_MEDIA:
            raise ValueError("JudgmentDebtSeed.observation_medium must be workbench_solid or eevee")
        if self.lifecycle not in JUDGMENT_DEBT_LIFECYCLES:
            raise ValueError("JudgmentDebtSeed.lifecycle must be persistent, layer, or window")
        _require_digest(self.bundle_digest, "JudgmentDebtSeed.bundle_digest")
        families = _require_text_tuple(self.carrier_families, "JudgmentDebtSeed.carrier_families")
        unknown = sorted(set(families) - RENDERED_CARRIER_FAMILIES)
        if unknown:
            raise ValueError("JudgmentDebtSeed.carrier_families contains unsupported families: " + ", ".join(unknown))
        if self.observation_medium == "workbench_solid" and set(families) != {"mesh"}:
            raise ValueError("JudgmentDebtSeed workbench_solid observation requires exactly the mesh carrier family")
        identity = {
            "schema": "vfx-harness.judgment-debt-id/v1",
            "requirement_id": self.requirement_id,
            "claim_kind": self.claim_kind,
            "property": self.property,
            "axes": sorted(self.axes),
            "subject_roles": sorted(self.subject_roles),
            "judge_points": [
                point.as_dict() for point in sorted(self.judge_points, key=lambda point: (point.frame, point.ref))
            ],
        }
        object.__setattr__(self, "debt_id", "jd-" + _canonical_digest(identity))

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "debt_id": self.debt_id,
            "requirement_id": self.requirement_id,
            "statement": self.statement,
            "decision_strength": self.decision_strength,
            "claim_kind": self.claim_kind,
            "property": self.property,
            "owner_layer": self.owner_layer,
            "fault_owner": self.fault_owner,
            "subject_roles": sorted(self.subject_roles),
            "axes": sorted(self.axes),
            "judge_points": [
                point.as_dict() for point in sorted(self.judge_points, key=lambda point: (point.frame, point.ref))
            ],
            "observation_medium": self.observation_medium,
            "lifecycle": self.lifecycle,
            "bundle_digest": self.bundle_digest,
            "carrier_families": sorted(self.carrier_families),
        }
        return {**payload, "seed_digest": _canonical_digest(payload)}

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        payload.pop("seed_digest")
        return _canonical_digest(payload)

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentDebtSeed:
        fields = (
            "debt_id",
            "requirement_id",
            "statement",
            "decision_strength",
            "claim_kind",
            "property",
            "owner_layer",
            "fault_owner",
            "subject_roles",
            "axes",
            "judge_points",
            "observation_medium",
            "lifecycle",
            "bundle_digest",
            "carrier_families",
            "seed_digest",
        )
        row = _record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            requirement_id=row["requirement_id"],
            statement=row["statement"],
            decision_strength=row["decision_strength"],
            claim_kind=row["claim_kind"],
            property=row["property"],
            owner_layer=row["owner_layer"],
            fault_owner=row["fault_owner"],
            subject_roles=_text_list(row["subject_roles"], f"{where}.subject_roles"),
            axes=_text_list(row["axes"], f"{where}.axes"),
            judge_points=tuple(
                JudgmentPoint.from_dict(point, f"{where}.judge_points[{index}]")
                for index, point in enumerate(_list(row["judge_points"], f"{where}.judge_points"))
            ),
            observation_medium=row["observation_medium"],
            lifecycle=row["lifecycle"],
            bundle_digest=row["bundle_digest"],
            carrier_families=_text_list(row["carrier_families"], f"{where}.carrier_families"),
        )
        if row["debt_id"] != candidate.debt_id:
            raise ValueError(f"{where}.debt_id is stale; expected {candidate.debt_id!r}, found {row['debt_id']!r}")
        _require_canonical_digest(row["seed_digest"], candidate.digest, where, "seed_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class JudgmentProvider:
    """One explicit promise that a layer contributes a rendered carrier."""

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-provider/v1"
    id: str
    layer_id: str
    carrier_family: str
    subject_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.id, "JudgmentProvider.id")
        _require_text(self.layer_id, "JudgmentProvider.layer_id")
        if self.carrier_family not in RENDERED_CARRIER_FAMILIES:
            raise ValueError("JudgmentProvider.carrier_family must be mesh, volume, or compositor")
        _require_roles(self.subject_roles, "JudgmentProvider.subject_roles")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "id": self.id,
            "layer_id": self.layer_id,
            "carrier_family": self.carrier_family,
            "subject_roles": sorted(self.subject_roles),
        }
        return {**payload, "provider_digest": _canonical_digest(payload)}

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        payload.pop("provider_digest")
        return _canonical_digest(payload)

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentProvider:
        row = _record(
            value, where, cls.SCHEMA, ("id", "layer_id", "carrier_family", "subject_roles", "provider_digest")
        )
        candidate = cls(
            row["id"],
            row["layer_id"],
            row["carrier_family"],
            _text_list(row["subject_roles"], f"{where}.subject_roles"),
        )
        _require_canonical_digest(row["provider_digest"], candidate.digest, where, "provider_digest")
        return candidate


def _provider_rows(value: Any, where: str) -> tuple[JudgmentProvider, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{where} must be a sequence of JudgmentProvider values")
    providers = tuple(value)
    if any(not isinstance(provider, JudgmentProvider) for provider in providers):
        raise ValueError(f"{where} must contain JudgmentProvider values")
    ids = [provider.id for provider in providers]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{where} contains duplicate ids")
    return providers


def _validate_provider_closure(
    subject_roles: tuple[str, ...],
    carrier_families: tuple[str, ...],
    bindings: tuple[tuple[str, tuple[str, ...]], ...],
    providers: tuple[JudgmentProvider, ...],
    where: str,
) -> None:
    subjects, referenced = _binding_references(bindings, f"{where}.subject_provider_ids")
    if subjects != set(subject_roles):
        raise ValueError(f"{where} binding subjects must exactly match subject_roles")
    by_id = {provider.id: provider for provider in providers}
    if len(by_id) != len(providers) or referenced != set(by_id):
        raise ValueError(f"{where} providers must exactly equal the subject bindings")
    for subject, provider_ids in bindings:
        for provider_id in provider_ids:
            provider = by_id[provider_id]
            if provider.carrier_family not in carrier_families or not any(
                _subject_matches_provider(subject, role) for role in provider.subject_roles
            ):
                raise ValueError(
                    f"{where} provider {provider_id!r} does not match subject "
                    f"{subject!r} and its allowed carrier families"
                )


@dataclass(frozen=True, slots=True)
class ProviderActivation:
    """Exact earliest DAG prefix and provider promises covering typed subjects."""

    SCHEMA: ClassVar[str] = "vfx-harness.provider-activation/v1"
    owner_layer: str
    subject_roles: tuple[str, ...]
    carrier_families: tuple[str, ...]
    activates_at: str
    subject_provider_ids: tuple[tuple[str, tuple[str, ...]], ...]
    providers: tuple[JudgmentProvider, ...]

    def __post_init__(self) -> None:
        _require_text(self.owner_layer, "ProviderActivation.owner_layer")
        _require_roles(self.subject_roles, "ProviderActivation.subject_roles")
        families = _require_text_tuple(self.carrier_families, "ProviderActivation.carrier_families")
        if set(families) - RENDERED_CARRIER_FAMILIES:
            raise ValueError("ProviderActivation.carrier_families contains unsupported families")
        _require_text(self.activates_at, "ProviderActivation.activates_at")
        providers = _provider_rows(self.providers, "ProviderActivation.providers")
        if not providers:
            raise ValueError("ProviderActivation.providers must be non-empty")
        _validate_provider_closure(
            self.subject_roles, families, self.subject_provider_ids, providers, "ProviderActivation"
        )

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(provider.id for provider in self.providers))

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "owner_layer": self.owner_layer,
            "subject_roles": sorted(self.subject_roles),
            "carrier_families": sorted(self.carrier_families),
            "activates_at": self.activates_at,
            "subject_provider_ids": [
                {"subject": subject, "provider_ids": sorted(ids)} for subject, ids in sorted(self.subject_provider_ids)
            ],
            "providers": [provider.as_dict() for provider in sorted(self.providers, key=lambda row: row.id)],
        }
        return {**payload, "provider_activation_digest": _canonical_digest(payload)}

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        payload.pop("provider_activation_digest")
        return _canonical_digest(payload)

    @classmethod
    def from_dict(cls, value: Any, where: str) -> ProviderActivation:
        row = _record(
            value,
            where,
            cls.SCHEMA,
            (
                "owner_layer",
                "subject_roles",
                "carrier_families",
                "activates_at",
                "subject_provider_ids",
                "providers",
                "provider_activation_digest",
            ),
        )
        providers = tuple(
            JudgmentProvider.from_dict(provider, f"{where}.providers[{index}]")
            for index, provider in enumerate(_list(row["providers"], f"{where}.providers"))
        )
        candidate = cls(
            row["owner_layer"],
            _text_list(row["subject_roles"], f"{where}.subject_roles"),
            _text_list(row["carrier_families"], f"{where}.carrier_families"),
            row["activates_at"],
            _subject_bindings_from_dict(row["subject_provider_ids"], f"{where}.subject_provider_ids"),
            providers,
        )
        _require_canonical_digest(
            row["provider_activation_digest"], candidate.digest, where, "provider_activation_digest"
        )
        return candidate


@dataclass(frozen=True, slots=True)
class JudgmentProviderBinding:
    """Debt-specific link from a seed to its generic provider activation."""

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-provider-binding/v1"
    seed_digest: str
    activates_at: str
    subject_provider_ids: tuple[tuple[str, tuple[str, ...]], ...]
    provider_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_digest(self.seed_digest, "JudgmentProviderBinding.seed_digest")
        _require_text(self.activates_at, "JudgmentProviderBinding.activates_at")
        _subjects, referenced = _binding_references(
            self.subject_provider_ids, "JudgmentProviderBinding.subject_provider_ids"
        )
        if referenced != _validate_digest_pairs(self.provider_digests, "JudgmentProviderBinding.provider_digests"):
            raise ValueError("JudgmentProviderBinding provider ids must exactly equal the subject bindings")

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(provider_id for provider_id, _digest in self.provider_digests))

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "seed_digest": self.seed_digest,
            "activates_at": self.activates_at,
            "subject_provider_ids": [
                {"subject": subject, "provider_ids": sorted(ids)} for subject, ids in sorted(self.subject_provider_ids)
            ],
            "provider_digests": [
                {"provider_id": provider_id, "digest": digest} for provider_id, digest in sorted(self.provider_digests)
            ],
        }
        return {**payload, "binding_digest": _canonical_digest(payload)}

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        payload.pop("binding_digest")
        return _canonical_digest(payload)

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentProviderBinding:
        row = _record(
            value,
            where,
            cls.SCHEMA,
            ("seed_digest", "activates_at", "subject_provider_ids", "provider_digests", "binding_digest"),
        )
        candidate = cls(
            row["seed_digest"],
            row["activates_at"],
            _subject_bindings_from_dict(row["subject_provider_ids"], f"{where}.subject_provider_ids"),
            _digest_pairs_from_dict(row["provider_digests"], f"{where}.provider_digests", "provider_id"),
        )
        _require_canonical_digest(row["binding_digest"], candidate.digest, where, "binding_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class JudgmentDebtDefinition:
    SCHEMA: ClassVar[str] = "vfx-harness.judgment-debt-definition/v1"
    seed: JudgmentDebtSeed
    providers: tuple[JudgmentProvider, ...]
    binding: JudgmentProviderBinding

    def __post_init__(self) -> None:
        if not isinstance(self.seed, JudgmentDebtSeed):
            raise ValueError("JudgmentDebtDefinition.seed must be JudgmentDebtSeed")
        providers = _provider_rows(self.providers, "JudgmentDebtDefinition.providers")
        if not providers or not isinstance(self.binding, JudgmentProviderBinding):
            raise ValueError("JudgmentDebtDefinition requires providers and a JudgmentProviderBinding")
        if self.binding.seed_digest != self.seed.digest:
            raise ValueError("JudgmentDebtDefinition.binding seed digest does not match the seed")
        if dict(self.binding.provider_digests) != {provider.id: provider.digest for provider in providers}:
            raise ValueError("JudgmentDebtDefinition binding does not match the exact provider promises")
        _validate_provider_closure(
            self.seed.subject_roles,
            self.seed.carrier_families,
            self.binding.subject_provider_ids,
            providers,
            "JudgmentDebtDefinition",
        )

    @property
    def debt_id(self) -> str:
        return self.seed.debt_id

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "debt_id": self.debt_id,
            "seed": self.seed.as_dict(),
            "providers": [provider.as_dict() for provider in sorted(self.providers, key=lambda row: row.id)],
            "binding": self.binding.as_dict(),
        }
        return {**payload, "definition_digest": _canonical_digest(payload)}

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        payload.pop("definition_digest")
        return _canonical_digest(payload)

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentDebtDefinition:
        row = _record(value, where, cls.SCHEMA, ("debt_id", "seed", "providers", "binding", "definition_digest"))
        seed = JudgmentDebtSeed.from_dict(row["seed"], f"{where}.seed")
        providers = tuple(
            JudgmentProvider.from_dict(provider, f"{where}.providers[{index}]")
            for index, provider in enumerate(_list(row["providers"], f"{where}.providers"))
        )
        candidate = cls(seed, providers, JudgmentProviderBinding.from_dict(row["binding"], f"{where}.binding"))
        if row["debt_id"] != candidate.debt_id:
            raise ValueError(f"{where}.debt_id does not match its seed")
        _require_canonical_digest(row["definition_digest"], candidate.digest, where, "definition_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class JudgmentDebtState:
    SCHEMA: ClassVar[str] = "vfx-harness.judgment-debt-state/v2"
    definition_digest: str
    status: str
    evidence_digest: str | None = None
    activation_digest: str | None = None
    payment_generation_digest: str | None = None

    def __post_init__(self) -> None:
        _require_digest(self.definition_digest, "JudgmentDebtState.definition_digest")
        if self.status not in JUDGMENT_DEBT_STATUSES:
            raise ValueError("JudgmentDebtState.status must be pending_not_due, due, satisfied, or falsified")
        if self.status in TERMINAL_JUDGMENT_DEBT_STATUSES:
            _require_digest(self.evidence_digest, "JudgmentDebtState.evidence_digest")
        elif self.evidence_digest is not None:
            raise ValueError("non-terminal JudgmentDebtState must omit evidence_digest")
        if self.status == "pending_not_due" and (
            self.activation_digest is not None
            or self.payment_generation_digest is not None
        ):
            raise ValueError(
                "pending_not_due JudgmentDebtState must omit activation and payment generation"
            )
        if self.status != "pending_not_due" and (
            self.activation_digest is None
            or self.payment_generation_digest is None
        ):
            raise ValueError(
                "due or terminal JudgmentDebtState requires activation and payment generation"
            )
        if self.activation_digest is not None:
            _require_digest(self.activation_digest, "JudgmentDebtState.activation_digest")
        if self.payment_generation_digest is not None:
            _require_digest(
                self.payment_generation_digest,
                "JudgmentDebtState.payment_generation_digest",
            )

    @classmethod
    def pending(cls, definition: JudgmentDebtDefinition) -> JudgmentDebtState:
        if not isinstance(definition, JudgmentDebtDefinition):
            raise ValueError("definition must be a JudgmentDebtDefinition")
        return cls(definition.digest, "pending_not_due")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "definition_digest": self.definition_digest,
            "status": self.status,
            "evidence_digest": self.evidence_digest,
            "activation_digest": self.activation_digest,
            "payment_generation_digest": self.payment_generation_digest,
        }
        return {**payload, "state_digest": _canonical_digest(payload)}

    @property
    def digest(self) -> str:
        payload = self.as_dict()
        payload.pop("state_digest")
        return _canonical_digest(payload)

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentDebtState:
        row = _record(
            value,
            where,
            cls.SCHEMA,
            (
                "definition_digest",
                "status",
                "evidence_digest",
                "activation_digest",
                "payment_generation_digest",
                "state_digest",
            ),
        )
        candidate = cls(
            row["definition_digest"],
            row["status"],
            row["evidence_digest"],
            row["activation_digest"],
            row["payment_generation_digest"],
        )
        _require_canonical_digest(row["state_digest"], candidate.digest, where, "state_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class JudgmentDebtActivation:
    """JIT payer identity linked to one exact compiled debt definition."""

    SCHEMA: ClassVar[str] = "vfx-harness.judgment-debt-activation/v1"
    debt_id: str
    definition_digest: str
    payer_layer: str
    payer_unit_digests: tuple[tuple[str, str], ...]
    subject_roles: tuple[str, ...]
    carrier_families: tuple[str, ...]
    provider_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_text(self.debt_id, "JudgmentDebtActivation.debt_id")
        _require_digest(self.definition_digest, "JudgmentDebtActivation.definition_digest")
        _require_text(self.payer_layer, "JudgmentDebtActivation.payer_layer")
        _validate_digest_pairs(self.payer_unit_digests, "JudgmentDebtActivation.payer_unit_digests")
        _require_roles(self.subject_roles, "JudgmentDebtActivation.subject_roles")
        families = _require_text_tuple(self.carrier_families, "JudgmentDebtActivation.carrier_families")
        if set(families) - RENDERED_CARRIER_FAMILIES:
            raise ValueError("JudgmentDebtActivation.carrier_families contains unsupported families")
        _validate_digest_pairs(self.provider_digests, "JudgmentDebtActivation.provider_digests")

    @classmethod
    def for_definition(
        cls,
        definition: JudgmentDebtDefinition,
        *,
        payer_unit_digests: tuple[tuple[str, str], ...],
    ) -> JudgmentDebtActivation:
        if not isinstance(definition, JudgmentDebtDefinition):
            raise ValueError("definition must be a JudgmentDebtDefinition")
        return cls(
            definition.debt_id,
            definition.digest,
            definition.binding.activates_at,
            payer_unit_digests,
            tuple(sorted(definition.seed.subject_roles)),
            tuple(sorted(definition.seed.carrier_families)),
            tuple(sorted(definition.binding.provider_digests)),
        )

    def assert_matches(self, definition: JudgmentDebtDefinition) -> None:
        if not isinstance(definition, JudgmentDebtDefinition):
            raise ValueError("definition must be a JudgmentDebtDefinition")
        expected = (
            definition.debt_id,
            definition.digest,
            definition.binding.activates_at,
            set(definition.seed.subject_roles),
            set(definition.seed.carrier_families),
            dict(definition.binding.provider_digests),
        )
        found = (
            self.debt_id,
            self.definition_digest,
            self.payer_layer,
            set(self.subject_roles),
            set(self.carrier_families),
            dict(self.provider_digests),
        )
        if found != expected:
            raise ValueError("JudgmentDebtActivation is stale or does not match the exact debt definition")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "debt_id": self.debt_id,
            "definition_digest": self.definition_digest,
            "payer_layer": self.payer_layer,
            "payer_units": [
                {"unit_id": unit_id, "digest": digest} for unit_id, digest in sorted(self.payer_unit_digests)
            ],
            "subject_roles": sorted(self.subject_roles),
            "carrier_families": sorted(self.carrier_families),
            "providers": [
                {"provider_id": provider_id, "digest": digest} for provider_id, digest in sorted(self.provider_digests)
            ],
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "activation_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> JudgmentDebtActivation:
        fields = (
            "debt_id",
            "definition_digest",
            "payer_layer",
            "payer_units",
            "subject_roles",
            "carrier_families",
            "providers",
            "activation_digest",
        )
        row = _record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            row["debt_id"],
            row["definition_digest"],
            row["payer_layer"],
            _digest_pairs_from_dict(row["payer_units"], f"{where}.payer_units", "unit_id"),
            _text_list(row["subject_roles"], f"{where}.subject_roles"),
            _text_list(row["carrier_families"], f"{where}.carrier_families"),
            _digest_pairs_from_dict(row["providers"], f"{where}.providers", "provider_id"),
        )
        if row["activation_digest"] != candidate.digest:
            raise ValueError(f"{where}.activation_digest is stale; expected {candidate.digest!r}")
        return candidate
