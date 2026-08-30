"""Package-attribute lookup so tests can patch vfx_harness.agents.builder."""


def builder_package():
    # Late lookup is the deliberate monkeypatch seam for the public package facade.
    import vfx_harness.agents.builder as pkg  # noqa: PLC0415

    return pkg
