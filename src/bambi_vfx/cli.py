"""Unified command-line interface for the Bambi VFX harness."""

from __future__ import annotations

import argparse
import importlib
import sys

_COMMANDS = {
    "accept": "bambi_vfx.agents.acceptance:main",
    "asset": "bambi_vfx.agents.asset_builder:main",
    "build": "bambi_vfx.agents.builder:main",
    "evals": "bambi_vfx.evals:main",
    "inspect": "bambi_vfx.inspect_run:main",
    "plan": "bambi_vfx.agents.planner:main",
    "preflight": "bambi_vfx.preflight:main",
    "render": "bambi_vfx.render_shot:main",
    "run": "bambi_vfx.run_shot:main",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bambi",
        description="Agent-driven, evidence-checked Blender VFX pipeline.",
    )
    parser.add_argument("command", nargs="?", choices=sorted(_COMMANDS))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    namespace = parser.parse_args()
    if namespace.command is None:
        parser.print_help()
        return 0

    module_name, function_name = _COMMANDS[namespace.command].split(":", 1)
    command = getattr(importlib.import_module(module_name), function_name)
    sys.argv = [f"bambi {namespace.command}", *namespace.args]
    result = command()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
