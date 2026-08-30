"""Mutable builder session registers shared across submodules."""

from __future__ import annotations

import anyio

_FOCUS_RENDER_LOCK = anyio.Lock()
_ERRORS: list[str] = []
_RECIPES_USED: list[str] = []
_JOURNAL_INFO: dict = {}
_APPROACH: dict = {}
