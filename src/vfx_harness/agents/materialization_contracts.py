"""VFX materialization operation descriptions, independent of model transport."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MaterializationObservation:
    text: str
    refused: bool = False
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MaterializationOperation:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Awaitable[MaterializationObservation]]


def operation(name: str, description: str, input_schema: dict):
    def register(handler):
        return MaterializationOperation(name, description, input_schema, handler)
    return register


def observation(text: str, is_error: bool = False, *, data: dict | None = None) -> MaterializationObservation:
    return MaterializationObservation(text, refused=is_error, data={} if data is None else data)
