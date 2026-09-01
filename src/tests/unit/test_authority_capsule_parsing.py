from __future__ import annotations

from copy import deepcopy

import pytest

from tests.unit.test_authority_capsules import _global_documents
from vfx_harness.domain.authority_capsule_parsing import (
    parse_authority_capsule_set,
)
from vfx_harness.domain.authority_capsules import (
    AuthorityCapsuleError,
    compile_authority_capsules,
)


def test_immutable_capsule_set_round_trips_to_exact_typed_value() -> None:
    documents = _global_documents()
    compiled = compile_authority_capsules(documents, documents)

    assert parse_authority_capsule_set(compiled.as_dict()) == compiled


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["units"].append({"unexpected": True}),
            "fields mismatch",
        ),
        (
            lambda value: value["layers"][0].update(capsule_digest="f" * 64),
            "capsule_digest is stale",
        ),
        (
            lambda value: value["provenance"]["document_schemas"].update(
                {"unknown.json": "1"}
            ),
            "closed v1 vocabulary",
        ),
    ],
)
def test_immutable_capsule_set_parser_fails_closed(
    mutation,
    message: str,
) -> None:
    documents = _global_documents()
    value = deepcopy(compile_authority_capsules(documents, documents).as_dict())
    mutation(value)

    with pytest.raises(AuthorityCapsuleError, match=message):
        parse_authority_capsule_set(value)
