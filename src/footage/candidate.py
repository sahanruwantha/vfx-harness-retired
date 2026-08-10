"""The provider-agnostic footage query and candidate types.

These are the seam every backend meets. A provider turns a `FootageQuery` (what a
shot needs on screen, plus acquisition constraints) into `FootageCandidate`s (real,
addressable clips). Nothing here knows about any specific API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Where a clip comes from, which the storyboard cares about: an archival news/TV
# record, a licensed broadcast clip, generic stock b-roll, or an open web video.
SourceClass = Literal["archival", "broadcast", "stock", "web"]


@dataclass(frozen=True)
class FootageQuery:
    """What a single shot needs, expressed so any provider can search for it.

    `need` is the plain-language description of what must be on screen (or audible);
    it is always usable. The rest narrow the search when a provider can honour them.
    """

    need: str
    keywords: tuple[str, ...] = ()
    source_classes: tuple[SourceClass, ...] = ()  # empty => any class is acceptable
    era: str | None = None  # e.g. "1987", "1980s", "pre-1990"
    spoken_phrase: str | None = None  # for subtitle-searchable providers (filmot)
    max_results: int = 8

    def terms(self) -> str:
        """The free-text query string: explicit keywords if given, else the need."""
        if self.keywords:
            return " ".join(self.keywords)
        return self.need.strip()

    def wants(self, source_class: SourceClass) -> bool:
        """True if a provider of `source_class` should run for this query."""
        return not self.source_classes or source_class in self.source_classes


@dataclass(frozen=True)
class FootageCandidate:
    """One acquirable clip. `license=None` means licence is UNKNOWN, not that it is free.

    A candidate is a lead, not a cleared asset: the sourcer must treat an unknown
    licence as a gate to flag, mirroring the pipeline's `[VERIFY]` discipline.
    """

    provider: str
    source_class: SourceClass
    title: str
    url: str  # canonical page / watch URL a human can open
    media_url: str | None = None  # direct downloadable media, when the provider exposes it
    identifier: str | None = None  # provider-native id (IA identifier, video id, page id)
    date: str | None = None  # publish/created date or year, as the provider reports it
    duration_seconds: float | None = None
    license: str | None = None  # licence URL or short label; None => unknown
    provenance: str | None = None  # original outlet / uploader / collection
    thumbnail_url: str | None = None
    match_notes: str = ""  # why this fits the shot (the agent fills this during vetting)
    extra: dict = field(default_factory=dict)

    @property
    def license_known(self) -> bool:
        return bool(self.license)

    def to_line(self) -> str:
        """A compact, machine-and-human-readable one-block summary for the tool result."""
        lic = self.license or "UNKNOWN — must clear before use"
        dur = f"{self.duration_seconds:.0f}s" if self.duration_seconds else "?"
        bits = [
            f"- **{self.title}**  ({self.source_class} · {self.provider})",
            f"  url: {self.url}",
        ]
        if self.media_url:
            bits.append(f"  media: {self.media_url}")
        bits.append(f"  date: {self.date or '?'} · duration: {dur} · licence: {lic}")
        if self.provenance:
            bits.append(f"  provenance: {self.provenance}")
        if self.match_notes:
            bits.append(f"  note: {self.match_notes}")
        return "\n".join(bits)
