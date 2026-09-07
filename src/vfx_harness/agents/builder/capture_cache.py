"""Reuse a rendered plate across composed groups that are looking at the same picture.

One finalization runs a group per judgment debt. Groups differing only in which debt they
pay replay the same scripts and render the same frame in the same mode, and the render is
the expensive half. This keeps the plates and hands the same bytes to each group under its
own name.

Two properties make reuse safe rather than merely cheap:

* **A hit is verified, not trusted.** The cached file must still exist and still hash to
  the digest its receipt claims. A dictionary lookup is not evidence about a file, and the
  cost of being wrong here is one group judging another group's picture.
* **Only the picture is shared.** Each group keeps its own locator, its own receipt row,
  its own claims and its own verdict. Nothing about the judgment is cached.

Copying the bytes to each group's own locator preserves what a reader expects to find, and
also makes the duplicates byte-identical where separate renders of one scene are not: PNG
metadata differs per write, so today's duplicate plates share every pixel and no SHA-256.
A later digest-of-artifact check over these files becomes meaningful rather than
accidentally distinguishing identical pictures.

A miss always re-renders, so every failure mode of this class degrades to the behaviour
that predates it.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vfx_harness.domain.capture_equivalence import (
    capture_equivalence_key,
    capture_matches_key,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class CaptureCache:
    """Rendered plates from this finalization, keyed by what determined their pixels."""

    def __init__(self, shot_folder: Path | str) -> None:
        self._root = Path(shot_folder)
        self._entries: dict[str, tuple[str, dict[str, Any]]] = {}
        self.hits = 0
        self.renders = 0

    def key(
        self,
        *,
        replay_inputs: Sequence[Any],
        frame: int,
        mode: str,
        scale: float,
    ) -> str | None:
        """The equivalence key, or None when the inputs cannot identify a scene.

        Returning None rather than raising keeps an unusable key a cache miss instead of a
        finalization failure: this is an optimisation, and it must never be the reason a
        layer cannot seal.
        """
        try:
            return capture_equivalence_key(
                replay_inputs=replay_inputs, frame=frame, mode=mode, scale=scale
            )
        except (AttributeError, TypeError, ValueError):
            # AttributeError included deliberately: a replay input or dependency whose
            # shape this does not recognise must render, never key on what it managed to
            # read. A partially-read identity is the one failure that returns a wrong
            # picture instead of a slow one (HIR-0249).
            return None

    def reuse(
        self,
        key: str | None,
        *,
        destination_tag: str,
        milestone_id: str,
        frame: int,
        mode: str,
        scale: float,
    ) -> tuple[str, dict[str, Any]] | None:
        """The cached plate copied under this group's own name, or None to render."""
        if key is None:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        source_rel, receipt = entry
        if not capture_matches_key(receipt, frame=frame, mode=mode, scale=scale):
            return None
        source = self._root / source_rel
        if not source.is_file() or _sha256(source) != str(receipt.get("png_sha256") or ""):
            # The bytes moved or vanished since they were cached. Drop the entry and let
            # the caller render: a stale hit is the one outcome worse than no cache.
            self._entries.pop(key, None)
            return None
        destination = source.parent / f"{milestone_id}_{destination_tag}.png"
        if destination != source:
            shutil.copyfile(source, destination)
        self.hits += 1
        return destination.relative_to(self._root).as_posix(), dict(receipt)

    def record(self, key: str | None, render_rel: str, receipt: Mapping[str, Any]) -> None:
        """Remember a freshly rendered plate for later groups in this finalization."""
        self.renders += 1
        if key is None:
            return
        self._entries.setdefault(key, (str(render_rel), dict(receipt)))


__all__ = ["CaptureCache"]
