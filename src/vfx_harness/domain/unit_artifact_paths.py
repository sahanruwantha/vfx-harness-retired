"""Identity-derived work-unit artifact paths shared by construction and replay."""

from vfx_harness.domain.field_parsing import identifier


def canonical_unit_script_path(layer_id: str, unit_id: str) -> str:
    """Return the sole replay artifact path for one unit."""
    layer = identifier(str(layer_id), "layer id")
    unit = identifier(str(unit_id), "unit id")
    directory = layer.zfill(2) if layer.isdigit() else layer
    return f"build/units/{directory}/{unit}.py"
