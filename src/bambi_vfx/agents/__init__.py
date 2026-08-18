"""Agent orchestration roles.

Each module owns one stage-level conversation and delegates deterministic work to the
core, Blender, asset, and evaluation services in the parent package.
"""

__all__ = ["acceptance", "asset_builder", "builder", "planner"]
