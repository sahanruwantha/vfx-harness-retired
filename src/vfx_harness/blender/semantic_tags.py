"""Pure semantic-tag mutations shared with Blender's injected helpers."""

from __future__ import annotations

from collections.abc import Callable


def tag_control(
    target,
    control: str,
    owner_layer: str | None,
    *,
    validate: Callable[[str], str],
):
    """Attach a control without overwriting an existing semantic role.

    Untagged shader/compositor nodes retain the historical role=control behavior so
    node-role contracts continue to resolve. Hosts that already have an object/material
    role keep that identity and gain an independent ``bvfx_control`` property.
    """
    control = validate(control)
    current_owner = target.get("bvfx_owner_layer")
    if (
        current_owner is not None
        and owner_layer is not None
        and str(current_owner) != str(owner_layer)
    ):
        raise ValueError(
            f"bvfx_control: {getattr(target, 'name', '<node>')!r} is owned by layer "
            f"{current_owner}; layer {owner_layer} may not retag its control"
        )
    if not target.get("bvfx_role"):
        target["bvfx_role"] = control
    target["bvfx_control"] = control
    if owner_layer is not None:
        target["bvfx_owner_layer"] = str(owner_layer)
    return target
