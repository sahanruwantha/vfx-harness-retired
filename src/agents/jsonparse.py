"""Lenient JSON extraction for agent replies — one place, because every agent needs it.

Models are asked for "one JSON object and nothing else" and almost always comply, but a single
delimiter glitch — a stray semicolon where a comma belongs, a trailing comma before a brace — makes
``json.loads`` reject the whole reply. When that reply is a rich structured decision (the scene
supervisor's per-element breakdown, a critic's verdict), throwing it away over one character is a
real defect: the caller silently falls back to a default and the model's actual answer is lost.

:func:`loads_json` extracts the outermost ``{...}`` and parses it strictly; only if that fails does it
apply a STRING-AWARE repair (semicolons outside strings → commas, trailing commas before ``}``/``]``
removed) and retry. Repairs never touch characters inside string values, so a note like
``"shafts; drift,"`` is preserved. Raises ``ValueError`` if there is no object or it is unrepairable.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCED_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)  # a ```json … ``` body
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)  # greedy: outermost { … } — correct for nested objects


def _repair(text: str) -> str:
    """Fix the two delimiter glitches models actually emit, only OUTSIDE string literals: a semicolon
    used as a member/element separator, and a trailing comma before a closing brace/bracket."""
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ";":  # a stray separator between members/elements → comma
            out.append(",")
        else:
            out.append(ch)
    # drop trailing commas (",]" / ", }") that are now outside any string
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def loads_json(text: str) -> dict[str, Any]:
    """Extract and parse the outermost JSON object in *text*, repairing common LLM glitches on failure.

    Raises ``ValueError`` when no object is present or it cannot be parsed even after repair.
    """
    fenced = _FENCED_BLOCK.search(text or "")  # if the reply is fenced, look inside the fence body
    scope = fenced.group(1) if fenced else (text or "")
    match = _OBJECT.search(scope)
    if not match:
        raise ValueError("no JSON object found")
    candidate = match.group(0)  # outermost {…}, so nested objects stay intact
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return json.loads(_repair(candidate))  # may still raise → caller's ValueError handler
