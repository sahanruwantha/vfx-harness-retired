"""One ordered, bounded image manifest for VFX critic transports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CriticImage:
    role: str
    path: str
    label: str


def compile_images(images: tuple[tuple[str, str], ...]) -> tuple[CriticImage, ...]:
    """Label precisely the images to attach; never truncate or invent optional slots."""
    roles = [role for role, _path in images]
    if roles[:2] != ["reference", "candidate"]:
        raise ValueError("critic images must begin with reference then candidate")
    limits = {"reference": 1, "candidate": 1, "focus": 2, "motion": 1, "prior": 1}
    if any(role not in limits or roles.count(role) > limits[role] for role in roles):
        raise ValueError("critic permits reference, candidate, at most two focus panels, one motion and one prior")
    labels = {"reference": "REFERENCE", "candidate": "CANDIDATE", "motion": "MOTION STRIP",
              "prior": "PREVIOUS ATTEMPT — CONTEXT ONLY"}
    result = []
    focus = 0
    for role, path in images:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"critic {role} requires an explicit image path; prepare the missing evidence")
        if role == "focus":
            focus += 1
            label = f"FOCUS PANEL {focus}"
        else:
            label = labels[role]
        result.append(CriticImage(role, path, label))
    return tuple(result)


def describe(images: tuple[CriticImage, ...]) -> str:
    return "ATTACHED IMAGE ORDER:\n" + "\n".join(
        f"Image {index}: {image.label} ({image.path})" for index, image in enumerate(images, 1)
    )
