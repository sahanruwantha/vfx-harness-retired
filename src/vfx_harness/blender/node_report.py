"""Pure text projection for Blender node trees.

Kept outside ``worker.py`` so socket-enumeration behavior can be regression-tested
without importing Blender's ``bpy`` module.
"""

from __future__ import annotations

import contextlib


def _format_socket_value(value) -> str:
    """Compact numeric text without rounding meaningful small values to zero."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".6g")
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        with contextlib.suppress(Exception):
            return "(" + ", ".join(_format_socket_value(item) for item in value) + ")"
    return str(value)


def format_node_tree(node_tree, title: str) -> str:
    """Report node types, readable input values, every output name, and links."""
    lines = [f"nodes for {title}:"]
    for node in node_tree.nodes:
        values = []
        for socket in node.inputs:
            if not socket.is_linked and hasattr(socket, "default_value"):
                values.append(
                    f"{socket.name}={_format_socket_value(socket.default_value)}"
                )
        outputs = [socket.name for socket in node.outputs]
        details = []
        if values:
            details.append(", ".join(values))
        if outputs:
            details.append("outputs=[" + ", ".join(outputs) + "]")
        lines.append(
            f"  [{node.type}] {node.name}"
            + (" | " + " | ".join(details) if details else "")
        )
    lines.append("links:")
    for link in node_tree.links:
        lines.append(
            f"  {link.from_node.name}.{link.from_socket.name} → "
            f"{link.to_node.name}.{link.to_socket.name}"
        )
    return "\n".join(lines)
