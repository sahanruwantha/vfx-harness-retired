"""Load a shot folder — the pipeline's input contract.

A shot is a directory containing:
  - brief.md   frontmatter (frames, fps, engine, palette, ...) + prose spec
  - refs/      milestone images + contact sheet (the visual target)

Everything downstream reads a `Shot` and never touches the folder directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

# refs shown to the planner, in the order it should reason about them:
# the whole-roll contact sheet first (overview), then the isolated milestones.
_REF_ORDER = [
    "roll_10fps_contactsheet",
    "M1_green",
    "M2_blackout",
    "M3_reveal",
    "M4_end",
]
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Shot:
    folder: Path
    frontmatter: dict
    body: str
    refs: list[Path] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.frontmatter.get("id", self.folder.name)

    @property
    def frames(self) -> int:
        return int(self.frontmatter["frames"])

    @property
    def fps(self) -> int:
        return int(self.frontmatter["fps"])

    @property
    def engine(self) -> str:
        return self.frontmatter.get("engine", "BLENDER_EEVEE_NEXT")

    @property
    def plan_path(self) -> Path:
        return self.folder / "plan.json"


def _ordered_refs(refs_dir: Path) -> list[Path]:
    if not refs_dir.is_dir():
        return []
    images = [p for p in refs_dir.iterdir() if p.suffix.lower() in _IMAGE_EXTS]
    rank = {name: i for i, name in enumerate(_REF_ORDER)}
    # known refs first, in canonical order; any extras after, alphabetically.
    return sorted(images, key=lambda p: (rank.get(p.stem, len(_REF_ORDER)), p.name))


def load_shot(folder: str | Path) -> Shot:
    folder = Path(folder).expanduser().resolve()
    brief = folder / "brief.md"
    if not brief.is_file():
        raise FileNotFoundError(f"no brief.md in shot folder: {folder}")

    text = brief.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{brief} has no `---` frontmatter block")

    frontmatter = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()

    for key in ("frames", "fps"):
        if key not in frontmatter:
            raise ValueError(f"{brief} frontmatter is missing required key: {key!r}")

    return Shot(
        folder=folder,
        frontmatter=frontmatter,
        body=body,
        refs=_ordered_refs(folder / "refs"),
    )
