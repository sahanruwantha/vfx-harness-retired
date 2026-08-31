"""Typed JIT materialization transaction failures."""


class MaterializationSelectionConflict(ValueError):
    """Materialization no longer has the exact plan/JIT selection it gated."""
