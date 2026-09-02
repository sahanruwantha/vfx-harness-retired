"""Pure structural classifiers for authoritative replay-script locators."""

from __future__ import annotations

from pathlib import PurePosixPath


def is_composed_layer_script_locator(locator: str | PurePosixPath) -> bool:
    """Return whether a normalized locator names a composed build artifact."""

    path = PurePosixPath(locator)
    return bool(
        path.parts
        and path.parts[0] == "build"
        and path.suffix == ".py"
        and not (len(path.parts) >= 2 and path.parts[1] == "units")
    )


__all__ = ["is_composed_layer_script_locator"]
