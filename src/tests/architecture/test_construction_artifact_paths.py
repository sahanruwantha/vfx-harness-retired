"""Construction and scripts share one canonical identity without a package cycle."""

from pathlib import PurePosixPath

import pytest

from vfx_harness.domain import refobs, unit_artifact_paths, work_units


def test_construction_and_unit_parser_use_one_path_derivation():
    assert refobs.canonical_unit_script_path is unit_artifact_paths.canonical_unit_script_path
    assert work_units.canonical_unit_script_path is unit_artifact_paths.canonical_unit_script_path


@pytest.mark.parametrize("layer", ["1", "01", "10", "camera", "layout-aux"])
def test_pointer_is_beside_the_canonical_script(layer):
    script = PurePosixPath(work_units.canonical_unit_script_path(layer, "source"))
    assert refobs.construction_pointer_relpath(layer, "source") == str(script.with_suffix(".construction.json"))
