"""Unified command-line interface for the VFX Harness harness."""

from __future__ import annotations

import argparse
import importlib
import sys

_COMMANDS = {
    "accept": "vfx_harness.agents.acceptance:main",
    "asset": "vfx_harness.agents.asset_builder:main",
    "build": "vfx_harness.agents.builder:main",
    "evals": "vfx_harness.evaluation.cli:main",
    "escalate": "vfx_harness.orchestration.escalate:main",
    "inspect": "vfx_harness.application.inspect_run:main",
    "plan": "vfx_harness.agents.planner:main",
    "preflight": "vfx_harness.application.preflight:main",
    "recover-environment": "vfx_harness.application.environment_recovery:main",
    "render": "vfx_harness.application.render_shot:main",
    "run": "vfx_harness.application.run_shot:main",
    "units": "vfx_harness.application.unit_admin:main",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vfx",
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
    sys.argv = [f"vfx {namespace.command}", *namespace.args]
    result = command()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
