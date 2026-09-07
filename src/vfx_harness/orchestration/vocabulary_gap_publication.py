"""Atomic vocabulary-gap publication shared by planning transports.

These records retain the existing shot-wide decision semantics. Publication is
not a finding that the registry is insufficient, nor an accepted plan decision.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path

from jsonschema import Draft202012Validator

from vfx_harness.domain import vocabulary_gaps
from vfx_harness.knowledge import planning_vocabulary
from vfx_harness.observability import prepared_publication
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

_TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}


def argument_schema(statements: Mapping[str, str]) -> dict:
    return {
        "type": "object", "properties": {
            "requirement_id": {"type": "string", "enum": sorted(statements)},
            "claim": _TEXT,
            "attempted": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                "type": "object", "properties": {
                    "kind": {"type": "string", "enum": sorted(planning_vocabulary.evidence_vocabulary()["kinds"])},
                    "why_it_cannot_certify": {**_TEXT, "maxLength": 512},
                }, "required": ["kind", "why_it_cannot_certify"], "additionalProperties": False,
            }},
            "note": {"type": "string", "maxLength": 2000},
        }, "required": ["requirement_id", "claim", "attempted"], "additionalProperties": False,
    }


def validate_arguments(arguments: dict, statements: Mapping[str, str]) -> None:
    errors = list(Draft202012Validator(argument_schema(statements)).iter_errors(arguments))
    if errors:
        raise ValueError("vocabulary gap schema: " + "; ".join(error.message for error in errors[:3]))
    if arguments["claim"] != statements[arguments["requirement_id"]]:
        raise ValueError("vocabulary gap claim must equal the exact authored requirement statement")
    attempted = arguments["attempted"]
    if len({row["kind"] for row in attempted}) != len(attempted):
        raise ValueError("vocabulary gap attempted kinds must be distinct")
    if any(not row["why_it_cannot_certify"].strip() for row in attempted):
        raise ValueError("vocabulary gap requires a nonempty reason for each attempted kind")


def _records(payload: bytes | None) -> list[dict]:
    records = []
    ids = set()
    for line in (payload or b"").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (not isinstance(row, dict) or set(row) != {
                "schema", "id", "requirement_id", "claim", "attempted", "note", "run_id"}
                or row["schema"] != vocabulary_gaps.SCHEMA
                or any(not isinstance(row[key], str) or not row[key].strip()
                       for key in ("id", "requirement_id", "claim", "run_id"))
                or not isinstance(row["note"], str)
                or not isinstance(row["attempted"], list) or not row["attempted"]
                or any(not isinstance(item, dict) or set(item) != {"kind", "why_it_cannot_certify"}
                       or any(not isinstance(value, str) or not value.strip() for value in item.values())
                       for item in row["attempted"])):
            raise ValueError("invalid vocabulary-gap ledger row; repair through an explicit migration")
        if row["id"] in ids:
            raise ValueError("duplicate vocabulary-gap id; repair through an explicit migration")
        ids.add(row["id"])
        records.append(row)
    return records


def record_gap(
    *, shot: Path, run_id: str, arguments: dict, statements: Mapping[str, str],
    check_current: Callable[[], None], authority_binding: str,
    commit_guard: Callable[[], AbstractContextManager],
) -> dict:
    validate_arguments(arguments, statements)
    check_current()
    path = vocabulary_gaps.vocabulary_gaps_path(shot)
    content = {**arguments, "note": arguments.get("note", ""),
               "attempted": sorted(arguments["attempted"], key=lambda row: row["kind"])}

    def update(payload):
        records = _records(payload)
        for row in records:
            if all(row[key] == value for key, value in content.items()):
                return None, (row, False)
        existing_ids = {row["id"] for row in records}
        index = 1
        while f"VG-{index:03d}" in existing_ids:
            index += 1
        row = {"schema": vocabulary_gaps.SCHEMA, "id": f"VG-{index:03d}", **content, "run_id": run_id}
        prefix = (payload or b"").rstrip(b"\n")
        return prefix + (b"\n" if prefix else b"") + json.dumps(row, sort_keys=True).encode() + b"\n", (row, True)

    prepared = prepared_publication.prepare_file_update(shot, path, update, authority_binding=authority_binding)
    try:
        check_current()
        with commit_guard():
            if prepared.publication is not None:
                prepared_publication.commit_prepared_file(prepared.publication, authority_binding=authority_binding)
    finally:
        prepared_publication.discard_prepared_file(prepared.publication)
    payload = read_real_file(shot, path, "committed vocabulary-gap ledger")
    row, created = prepared.result
    if row not in _records(payload):
        raise ValueError("published vocabulary gap is absent from current ledger; inspect the interrupted operation")
    check_current()
    return {"record": row, "created": created, "ledger": str(path.relative_to(shot)),
            "ledger_sha256": digest(payload), "plan_authority_changed": False}
