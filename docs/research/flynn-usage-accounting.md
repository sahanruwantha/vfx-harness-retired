# Flynn inference accounting integration

The SDK working branch `codex/sqlite-runtime` now returns an explicit
`InferenceResult(call, usage)` and writes immutable per-operation accounting in
SQLite schema 3. VFX installs that named branch directly through its optional
`flynn` extra. The implementation commit is `a49265ef2adf18a49a3c7d359184ed83cf195467`.
The subsequent `3cd7d41872b0cca99495383fc1826516501e836e` updates only the SDK
guide and a deterministic timeout test; its production source is byte-identical.

## Ownership and semantics

Flynn owns invocation identity, provider/model and response/finish identity, token
counts, and durable accounting for successful and failed inference. Usage has
three states: known, unknown and not applicable. Scripted operations are not
applicable; a dispatched model request without complete usage is unknown. Partial
known counts remain visible. A missing report is separately unreported, including
process death and historical schema-2 invocations. None of these records asserts a
price, bill, domain verdict or token-budget enforcement.

VFX owns unit identity and publishes `reports/flynn-usage-<claim-id>.json` through
the existing run-artifact writer. This is a derived audit projection binding the
attempt claim and relative journal locator. It is written when the executor exits,
including handled failure and cancellation. Process death may prevent the JSON
projection; SQLite remains the accounting source and `SQLiteRun.inspect` can read
it without taking execution ownership. This report provides no acceptance or resume
authority. Existing unit receipts, cold replay and layer finalization are unchanged.

Schema 2 remains readable through the SDK's read-only audit interface, tested against
a golden exported from the released implementation. It cannot be reopened for
execution. Schema-3 run creation is required for new work; this does not migrate or
invalidate VFX's accepted-build authority.

ARC's episode summaries now derive usage from SQLite. DeepSeek callbacks remain
provider diagnostics and replay input, not episode token-count authority. Its direct
experimental adapter callers explicitly unwrap the new result's `.call`; those
standalone scripts retain their existing diagnostics and are not newly journaled.
Token limits, pricing, phase termination and visual-unit migration remain later work.

## Validation

- SDK: 102 tests; Ruff, formatting, mypy, scripted reopen example and package build
  passed. The isolated installed wheel passed 100 unit/integration tests under
  Python 3.13, as did the SDK checkout checks. VFX uses Python 3.11.
- ARC: the required SSH sync installed exactly `3cd7d41` and passed 169 offline tests;
  source/test Ruff and mypy passed. Unrelated concurrent ARC work is preserved.
- VFX: SSH installation provenance matches both `codex/sqlite-runtime` and `a49265e`.
  Complete-source Ruff passed. All **2,928 VFX tests passed** on unchanged source
  across four independent shards (593 + 907 + 717 + 711), including the native
  dependent-layer and failed-unit replay gates. The longest shard took 770.42 seconds.
  The 15 warnings are existing Pillow `getdata` deprecations. Logs:
  `/tmp/vfx-sqlite-regression-4npu3ik_`.
  VFX then refreshed the named branch to `3cd7d41`; every production Python file
  matched the tested `a49265e` package byte-for-byte. Installed provenance verified
  both that commit and the requested branch. All 14 focused contract/budget tests
  passed after the refresh.
- A repeated SDK check under concurrent Blender load exposed a pre-existing 100 ms
  timeout-test race: budget expiry before dispatch was correctly refused. The test
  now reschedules the real runtime deadline after the tool enters, deterministically
  proving unknown-effect preservation. All 102 SDK tests passed after that test fix.
- No paid inference was run for this dependency/accounting change.

ARC migration edits and its updated lock remain in the shared ARC worktree, unstaged;
that checkout was concurrently used for separate resource-evidence work. Only SDK
and VFX branch changes are committed by this task. The consumer migration diff is
saved at `/tmp/arc-flynn-usage-migration.patch` for review.
