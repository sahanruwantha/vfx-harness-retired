"""Strict standalone environment probe results with no shot-authority side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
    require_text,
    require_text_tuple,
    text_tuple_from_list,
)


@dataclass(frozen=True, slots=True)
class EnvironmentCheck:
    """One deterministic, secret-free preflight observation."""

    SCHEMA: ClassVar[str] = "vfx-harness.environment-check/v1"

    check_id: str
    passed: bool
    observed_digest: str
    expected: str
    found: str
    next_action: str

    def __post_init__(self) -> None:
        require_id(self.check_id, "EnvironmentCheck.check_id")
        if not isinstance(self.passed, bool):
            raise ValueError("EnvironmentCheck.passed must be a boolean")
        require_digest(self.observed_digest, "EnvironmentCheck.observed_digest")
        for name in ("expected", "found", "next_action"):
            require_text(getattr(self, name), f"EnvironmentCheck.{name}")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "check_id": self.check_id,
            "passed": self.passed,
            "observed_digest": self.observed_digest,
            "expected": self.expected,
            "found": self.found,
            "next_action": self.next_action,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "check_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> EnvironmentCheck:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "check_id",
                "passed",
                "observed_digest",
                "expected",
                "found",
                "next_action",
                "check_digest",
            ),
        )
        candidate = cls(
            check_id=row["check_id"],
            passed=row["passed"],
            observed_digest=row["observed_digest"],
            expected=row["expected"],
            found=row["found"],
            next_action=row["next_action"],
        )
        require_canonical_digest(row["check_digest"], candidate.digest, where, "check_digest")
        return candidate


@dataclass(frozen=True, slots=True)
class EnvironmentProbeSpec:
    """The exact versioned observation surface used by one environment probe."""

    SCHEMA: ClassVar[str] = "vfx-harness.preflight-probe-spec/v2"

    probe_id: str
    probe_revision: int
    check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_id(self.probe_id, "EnvironmentProbeSpec.probe_id")
        if (
            not isinstance(self.probe_revision, int)
            or isinstance(self.probe_revision, bool)
            or self.probe_revision < 1
        ):
            raise ValueError("EnvironmentProbeSpec.probe_revision must be a positive integer")
        object.__setattr__(
            self,
            "check_ids",
            require_text_tuple(self.check_ids, "EnvironmentProbeSpec.check_ids"),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "probe_id": self.probe_id,
            "probe_revision": self.probe_revision,
            "check_ids": list(self.check_ids),
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "probe_spec_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> EnvironmentProbeSpec:
        row = record(
            value,
            where,
            cls.SCHEMA,
            ("probe_id", "probe_revision", "check_ids", "probe_spec_digest"),
        )
        candidate = cls(
            probe_id=row["probe_id"],
            probe_revision=row["probe_revision"],
            check_ids=text_tuple_from_list(row["check_ids"], f"{where}.check_ids"),
        )
        require_canonical_digest(
            row["probe_spec_digest"],
            candidate.digest,
            where,
            "probe_spec_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class EnvironmentResult:
    """A complete standalone preflight result; it never creates shot authority."""

    SCHEMA: ClassVar[str] = "vfx-harness.environment-result/v2"

    probe_id: str
    checks: tuple[EnvironmentCheck, ...]
    probe_spec: EnvironmentProbeSpec | None = None

    def __post_init__(self) -> None:
        require_id(self.probe_id, "EnvironmentResult.probe_id")
        if not isinstance(self.checks, tuple) or not self.checks:
            raise ValueError("EnvironmentResult.checks must be a non-empty tuple")
        if any(not isinstance(check, EnvironmentCheck) for check in self.checks):
            raise ValueError("EnvironmentResult.checks must contain EnvironmentCheck values")
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("EnvironmentResult.checks contains duplicate check ids")
        object.__setattr__(self, "checks", tuple(sorted(self.checks, key=lambda check: check.check_id)))
        expected_ids = tuple(sorted(ids))
        if self.probe_spec is None:
            object.__setattr__(
                self,
                "probe_spec",
                EnvironmentProbeSpec(
                    probe_id=self.probe_id,
                    probe_revision=1,
                    check_ids=expected_ids,
                ),
            )
        elif not isinstance(self.probe_spec, EnvironmentProbeSpec):
            raise ValueError("EnvironmentResult.probe_spec must be an EnvironmentProbeSpec")
        elif (
            self.probe_spec.probe_id != self.probe_id
            or self.probe_spec.check_ids != expected_ids
        ):
            raise ValueError("EnvironmentResult.probe_spec must bind its exact probe and check IDs")

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    def _environment_payload(self) -> dict[str, Any]:
        assert self.probe_spec is not None
        return {
            "schema": "vfx-harness.environment-state/v2",
            "probe_id": self.probe_id,
            "probe_spec_digest": self.probe_spec.digest,
            "checks": [
                {
                    "check_id": check.check_id,
                    "passed": check.passed,
                    "observed_digest": check.observed_digest,
                }
                for check in self.checks
            ],
        }

    @property
    def environment_digest(self) -> str:
        return canonical_digest(self._environment_payload())

    def _payload(self) -> dict[str, Any]:
        assert self.probe_spec is not None
        return {
            "schema": self.SCHEMA,
            "probe_id": self.probe_id,
            "probe_spec": self.probe_spec.as_dict(),
            "ok": self.ok,
            "environment_digest": self.environment_digest,
            "checks": [check.as_dict() for check in self.checks],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "result_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> EnvironmentResult:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "probe_id",
                "probe_spec",
                "ok",
                "environment_digest",
                "checks",
                "result_digest",
            ),
        )
        checks = tuple(
            EnvironmentCheck.from_dict(item, f"{where}.checks[{index}]")
            for index, item in enumerate(list_value(row["checks"], f"{where}.checks"))
        )
        candidate = cls(
            probe_id=row["probe_id"],
            checks=checks,
            probe_spec=EnvironmentProbeSpec.from_dict(
                row["probe_spec"], f"{where}.probe_spec"
            ),
        )
        if row["ok"] is not candidate.ok:
            raise ValueError(f"{where}.ok is inconsistent with its checks")
        require_canonical_digest(
            row["environment_digest"],
            candidate.environment_digest,
            where,
            "environment_digest",
        )
        require_canonical_digest(row["result_digest"], candidate.digest, where, "result_digest")
        return candidate
