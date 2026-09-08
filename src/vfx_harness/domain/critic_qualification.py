"""Pure, closed records for explicitly selected measured critic profile sets."""

from __future__ import annotations

from pathlib import PurePosixPath

SCHEMA = "vfx-harness.critic-qualification/v2"
RATES = ("false_pass_rate", "false_failure_rate", "repeatability_failure_rate",
         "scope_leakage_rate", "irrelevant_change_sensitivity_rate")
PROFILE_KEYS = {"judge_model", "prompt_sha256", "evidence_shape", "native_invocation_sha256",
                "budgets", "metrics", "calibration_proof"}


def keys(value: object, expected: set[str], where: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{where} requires exactly {sorted(expected)}; select a complete measured profile set")


def digest(value: object, where: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{where} requires 64 lowercase hexadecimal characters")


def text(value: object, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} requires nonempty text")


def reference(value: object, where: str) -> None:
    keys(value, {"path", "sha256"}, where)
    path = value["path"]
    text(path, f"{where}.path")
    if (PurePosixPath(path).is_absolute() or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError(f"{where}.path requires a normalized relative source path")
    digest(value["sha256"], f"{where}.sha256")


def validate_profile(value: object, where: str) -> None:
    keys(value, PROFILE_KEYS, where)
    for key in ("judge_model", "evidence_shape"):
        text(value[key], f"{where}.{key}")
    for key in ("prompt_sha256", "native_invocation_sha256"):
        digest(value[key], f"{where}.{key}")
    keys(value["calibration_proof"], {"request", "evaluation"}, f"{where}.calibration_proof")
    for key, source in value["calibration_proof"].items():
        reference(source, f"{where}.calibration_proof.{key}")
    for key in ("budgets", "metrics"):
        keys(value[key], set(RATES), f"{where}.{key}")
        for name, rate in value[key].items():
            if type(rate) not in (int, float) or not 0 <= rate <= 1:
                raise ValueError(f"{where}.{key}.{name} requires a finite rate in [0,1]")
    for name in RATES:
        if value["metrics"][name] > value["budgets"][name]:
            raise ValueError(f"{where}.metrics.{name} exceeds its measured qualification budget")


def validate_record(value: object, where: str = "critic qualification") -> None:
    """Validate shape and rates only; the owning adapter must reopen every measured proof."""
    keys(value, {"schema", "suite", "claim_id", "selection", "profiles"}, where)
    if value["schema"] != SCHEMA:
        raise ValueError(f"{where} requires {SCHEMA}; requalify obsolete single-profile artifacts")
    for key in ("suite", "claim_id"):
        text(value[key], f"{where}.{key}")
    reference(value["selection"], f"{where}.selection")
    if not isinstance(value["profiles"], list) or not value["profiles"]:
        raise ValueError(f"{where}.profiles requires at least one measured profile")
    identities = []
    for index, profile in enumerate(value["profiles"]):
        validate_profile(profile, f"{where}.profiles[{index}]")
        identities.append(profile["native_invocation_sha256"])
    if identities != sorted(set(identities)):
        raise ValueError(f"{where}.profiles requires distinct profiles ordered by invocation digest")
