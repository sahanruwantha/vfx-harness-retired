"""Identity-safe filesystem locators for sealed layer outcomes.

Layer ids are semantic identifiers, not integers and not path fragments.  Encoding the
exact UTF-8 bytes keeps distinct ids distinct (including ``"1"`` and ``"01"``) while
ensuring slashes, dot segments, Unicode, and punctuation can never escape the owned
``plans/outcomes`` directory.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

OUTCOME_DIRECTORY = PurePosixPath("plans/outcomes")


def _layer_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("layer outcome id must be a non-empty trimmed string")
    return value


def layer_identity_segment(layer_id: str) -> str:
    """Return one injective, traversal-free filename segment for a layer id."""

    identity = _layer_id(layer_id)
    return "layer-" + identity.encode("utf-8").hex()


def layer_outcome_locator(layer_id: str) -> str:
    """Return the canonical shot-relative locator for one exact layer identity."""

    return (OUTCOME_DIRECTORY / f"{layer_identity_segment(layer_id)}.json").as_posix()


def layer_outcome_path(shot_folder: str | Path, layer_id: str) -> Path:
    """Resolve one canonical outcome path without interpreting the layer id as a path."""

    root = Path(shot_folder).expanduser().resolve()
    path = (root / layer_outcome_locator(layer_id)).resolve()
    if not path.is_relative_to(root):  # Defensive invariant if the locator changes later.
        raise ValueError("canonical layer outcome locator escapes the shot root")
    return path
