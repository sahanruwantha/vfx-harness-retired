# Repository architecture

The codebase follows a package-by-capability layout. Runtime code lives in `src/bambi_vfx/`;
tests, documentation, local shot inputs, and generated outputs stay outside the package.

```text
src/bambi_vfx/
  config.py          environment loading and typed runtime settings
  cli.py             unified command dispatcher
  agents/            planner, builder, acceptance, and asset-agent orchestration
  blender/           Blender process boundary, tools, and checks
  assets/            external asset providers and normalization
  eval/              offline evaluation and reproducibility checks
  recipes/           versioned knowledge used by agents
tests/                deterministic and integration test entry points
docs/                 architecture, findings, and development probes
shots/                local user inputs and run artifacts (gitignored)
evals/                local evaluation artifacts (gitignored)
renders/              disposable root-level renders (gitignored)
```

## Dependency direction

Core data and configuration modules must not import agent entry points. Agent entry points
may depend on core modules and capability packages. Provider adapters stay behind
`bambi_vfx.assets`; Blender-specific code stays behind `bambi_vfx.blender`. Evaluation code
may inspect the runtime package, but runtime modules must not depend on `bambi_vfx.eval`.

## Naming conventions

- Python modules, functions, and variables use `snake_case`.
- Classes and typed records use `PascalCase`.
- Constants and environment variables use `UPPER_SNAKE_CASE`.
- Project-owned environment variables use the existing `BVFX_` prefix.
- Public commands use `bambi <verb>`; `bambi-<verb>` commands are available for automation.
- Agent modules use role nouns (`planner.py`, `builder.py`, `acceptance.py`); provider
  modules use the provider name.
- Generated files are never imported as source code and belong under a shot's output tree.

## Configuration boundary

Importing `bambi_vfx` never loads files or mutates the process environment. Executable
entry points load `.env` through `bambi_vfx.config.load_environment()`. Resolution order is:

1. an explicit path passed by code;
2. the absolute path in `BVFX_ENV_FILE`;
3. `.env` at the repository root when running from a checkout.

Existing process variables always win. Secrets remain in the environment for SDKs to read
directly and are not copied into the typed `Settings` object or emitted by diagnostics.
