"""Materialization kickoff compiles the judgment-property rule from the sparse row."""

from __future__ import annotations

from vfx_harness.agents.planner import kickoff


def test_camera_layer_kickoff_names_camera_framing_as_its_only_judgment_property() -> None:
    block = kickoff._judgment_property_authority_block(
        {"id": "1", "jit": {"provides": {"camera": ["camera.*"]}}}
    )
    assert "['camera_framing', 'reference_identity', 'subject_appearance']" in block
    assert "must be 'camera_framing'" in block
    assert "refused here" in block


def test_form_layer_kickoff_sends_camera_framing_to_the_camera_owner() -> None:
    block = kickoff._judgment_property_authority_block({"id": "2", "jit": {"provides": {}}})
    assert "does not provide camera" in block
    assert "'subject_appearance' or 'reference_identity'" in block


def test_staging_schema_offers_only_payable_authorities() -> None:
    from vfx_harness.domain.work_units.authoring import work_unit_authoring_schema
    from vfx_harness.domain.work_units.parsing import CLAIM_AUTHORITIES, STAGEABLE_CLAIM_AUTHORITIES

    schema = work_unit_authoring_schema(stageable_authorities=STAGEABLE_CLAIM_AUTHORITIES)
    claim = schema["properties"]["evaluation"]["properties"]["claims"]["items"]
    assert claim["properties"]["authority"]["enum"] == ["advisory", "executable_required"]
    assert "qualified_qualitative_required" in claim["properties"]["authority"]["description"]
    everything = work_unit_authoring_schema()
    every_claim = everything["properties"]["evaluation"]["properties"]["claims"]["items"]
    assert every_claim["properties"]["authority"]["enum"] == sorted(CLAIM_AUTHORITIES)
    kickoff_text = kickoff._judgment_property_authority_block({"id": "2", "jit": {"provides": {}}})
    assert "Claim authority at staging: executable_required or advisory" in kickoff_text
