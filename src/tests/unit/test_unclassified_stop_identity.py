"""Two different causes get two fingerprints, and a falsified unit is a typed stop.

caesar and hansa both terminated on unclassified boundaries with byte-identical
identities:

    cause_fingerprint       4d94afd68547f51dca60a0e249897ca24907dfb3f975cac924fc98a6b08a6114
    normalized_facts_digest e6d66961ae63438a478afffd69160897f2cfb6a39d6486ea3a90aa0875c4754f
    finding_ids             ['unclassified-boundary-e6d66961ae63438a478a']

The facts were two constants, so one value covered five genuinely different causes
across two shots -- including an operator's own SIGTERM. The controller refuses a cause
fingerprint already dispatched in a shot, so the second real defect would be suppressed
as a repeat of the first (HIR-0214).
"""

from __future__ import annotations

import pytest

from vfx_harness.observability import unclassified_authority
from vfx_harness.orchestration.unit_state_queries import unresolved_falsification


def _facts(*, boundary: str, cause: str, exc: BaseException) -> dict:
    """The identity payload as _unclassified_stop_envelope now builds it."""
    resolved = unclassified_authority.closed_terminal_cause(cause)
    return {
        "schema": "vfx-harness.unclassified-boundary-facts/v2",
        "boundary": boundary,
        "invariant": "terminal_boundary_requires_typed_stop",
        "terminal_cause": resolved,
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "operator_initiated": resolved in unclassified_authority.OPERATOR_CAUSES,
    }


def test_five_distinct_causes_do_not_share_one_identity() -> None:
    """The exact five that collided, one per real terminal in the two shots."""
    from vfx_harness.domain.stop_envelope_primitives import canonical_digest

    observed = {
        canonical_digest(_facts(boundary="build", cause=cause, exc=exc))
        for cause, exc in (
            ("process_error", ValueError("mint")),
            ("process_error", RuntimeError("stale finalization claim")),
            ("build_truncated", RuntimeError("no SDK event")),
            ("interrupted", KeyboardInterrupt()),
            ("process_error", ValueError("cannot claim work unit")),
        )
    }
    assert len(observed) == 4, "only the two same-type same-cause rows may coincide"


def test_an_operator_stop_is_distinguishable_from_a_code_fault() -> None:
    operator = _facts(boundary="build", cause="interrupted", exc=KeyboardInterrupt())
    fault = _facts(boundary="build", cause="process_error", exc=ValueError("x"))

    assert operator["operator_initiated"] is True
    assert fault["operator_initiated"] is False
    assert operator != fault


def test_free_form_prose_cannot_enter_the_identity() -> None:
    """The cause is closed vocabulary; an unknown one collapses rather than leaking."""
    assert unclassified_authority.closed_terminal_cause("something new") == (
        "unclassified_terminal_cause"
    )
    a = _facts(boundary="build", cause="x", exc=ValueError("detail one"))
    b = _facts(boundary="build", cause="y", exc=ValueError("detail two"))
    assert a == b, "only the exception TYPE and the closed cause participate"


def test_a_falsified_unit_is_reported_rather_than_claimed(tmp_path, monkeypatch) -> None:
    """The claim refusal held the finding all along; now the driver reads it first."""
    from vfx_harness.orchestration import unit_state_queries

    finding = {"record_id": "hf-a43e90596de399af5dcb", "unit": "columns_unit"}
    state = {
        "units": {
            "columns_unit": {"status": "hypothesis_falsified", "falsification": finding},
            "dais_unit": {"status": "passed"},
        }
    }
    monkeypatch.setattr(unit_state_queries, "load", lambda folder, layer_id: state)

    assert unresolved_falsification(tmp_path, "2", "columns_unit") == finding
    assert unresolved_falsification(tmp_path, "2", "dais_unit") is None
    assert unresolved_falsification(tmp_path, "2", "absent_unit") is None


def test_a_falsified_unit_without_a_bound_finding_is_not_silently_claimable(
    tmp_path, monkeypatch
) -> None:
    from vfx_harness.orchestration import unit_state_queries

    monkeypatch.setattr(
        unit_state_queries,
        "load",
        lambda folder, layer_id: {"units": {"columns_unit": {"status": "hypothesis_falsified"}}},
    )
    # No finding to report, so the claim's own refusal remains the boundary.
    assert unresolved_falsification(tmp_path, "2", "columns_unit") is None


@pytest.mark.parametrize("status", ["building", "frozen", "retryable", "superseded"])
def test_only_the_falsified_state_short_circuits_the_claim(
    tmp_path, monkeypatch, status
) -> None:
    from vfx_harness.orchestration import unit_state_queries

    monkeypatch.setattr(
        unit_state_queries,
        "load",
        lambda folder, layer_id: {
            "units": {"u": {"status": status, "falsification": {"record_id": "hf-x"}}}
        },
    )
    assert unresolved_falsification(tmp_path, "2", "u") is None
