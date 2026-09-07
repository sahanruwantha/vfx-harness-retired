# Durable Flynn output-token budgets

The Flynn engine can now receive `RunLimits(..., output_tokens=N)`. This is an
optional model-output cap selected by the harness, in addition to its existing
call, tool, external-action and wall-time limits. VFX applies the cap per claimed
unit; ARC applies it per episode. Allocating budgets across attempts or layers
remains harness policy. Input tokens remain accounted
for but are not capped; the current adapter has no verified pre-request input
bound. This is not a USD budget.

## Mechanism and ownership

The SDK's no-I/O `plan_output(request, available)` adapter contract proposes an
`OutputReservation`. SQLite schema 4 inserts that reservation atomically with the
operation and inference reservation, before any model request. DeepSeek sends the
reserved ceiling as `max_tokens`, bounded by its configured per-call maximum.
Unsupported adapters cannot run under an output cap.

Known output usage settles the reservation and releases its unused portion,
including when a model response is rejected. Unknown or unreported output holds
the full reservation and prevents further model calls, including after reopening
or explicit recovery of an undispatched operation. Known output can settle even
when input usage is unknown. A provider that exceeds its declared bound or an
adapter that misreports its invocation kind leaves a durable breach: retain usage,
refuse its proposed tool, and block later model calls. The SDK cannot undo external
provider violations, and its guarantee relies on the adapter honoring the ceiling.

Scripted work reserves zero output tokens and remains subject to its other budgets
and VFX authority checks. This permits already-authorized deterministic canonical
replay when model-output capacity is exhausted. It never permits another model
call, a VFX acceptance bypass, or session resume.

VFX's derived usage projection is now `vfx-harness.flynn-usage/v2` and includes
`output_budget` with limit, available, spent, held, unresolved and breached counts.
VFX keeps the unit policy, cold replay, receipt writers and domain acceptance.
There is no new default cap or change to the production CLI's selected engine.
ARC separately exposes `--output-tokens` on its visual and official episode commands.

Schema 2 and 3 journals remain read-only audit evidence; only schema 4 may execute.
A golden fixture pins the schema-3 request key set and existing known usage. This
is not a migration of accepted VFX build authority or a digest-schema cutover.

## Validation

- SDK implementation: `d87ef29d7a57b90efae00f95aa43ee7a378de4c0`; 113 SDK tests,
  Ruff, formatting, mypy, package build and scripted example passed. The isolated
  installed wheel passed 111 unit/integration tests outside the checkout.
- SDK follow-up `103d54ac41fcfee7edcd344fa3ef48b58ab8a1d0` strengthens the historical
  fixture only. Its production source matches the implementation commit exactly,
  and all 113 SDK tests pass.
- ARC offline tests exercise zero, known and unknown output budgets, including
  scripted RESET and refusal to spend on an uncertain correction. The first required
  SSH gate installed `d87ef29` and passed 195 tests.
- VFX's native unit lifecycle fixture consumes exactly 12 output tokens across four
  mock model responses, then completes scripted canonical replay with zero tokens
  available. It checks the derived report against the durable budget and existing
  receipt behavior for passing and failed candidates. All **2,928 VFX tests passed**
  on frozen source against SSH-installed `d87ef29`: 593 + 907 + 717 + 711 across four
  shards. The longest shard took 751.42 seconds. The 15 warnings are existing Pillow
  deprecations. Logs: `/tmp/vfx-sqlite-regression-jpzwrks2`.
- No paid inference is part of these checks. Input limits and USD settlement remain
  separate work requiring a verified bound or pricing contract.


## Final durable-limit guard

SDK `174d8d9cd1e098e783b45aa6dc58b117eda03766` additionally refuses missing or invalid
schema-4 output limits before any invocation. A missing field must not silently
become an uncapped run; booleans, negative counts, non-integral numbers and strings
are refused too. This follow-up changes no consumer signatures or import structure.
All **118 SDK tests** pass, as do Ruff, formatting, mypy and the package build; the
isolated installed wheel passes **116 unit/integration tests**.

The required final ARC SSH installation gate installed exactly `174d8d9` and passed
**207 offline tests**. Source/test Ruff and mypy passed, and both CLI help surfaces
expose `--output-tokens`. VFX refreshed the named `codex/sqlite-runtime` branch and
verified exactly `174d8d9`; all **32 focused Flynn tests passed** in 192.79 seconds,
including real confinement, per-unit lifecycle and dependent-layer replay. The full
2,928-test suite above ran before this final SDK-only durable-limit validation guard;
no VFX source changed between that full run and the focused final run. Final focused
log: `/tmp/vfx-output-budget-final-focused.log`.

The budget view describes reservations and their settlement. Overall observed token
consumption, including uncapped runs, remains in the separate usage summary.

ARC migration changes and its updated lock remain unstaged in the shared ARC
worktree, alongside the preceding accounting migration. This task commits and
pushes the SDK and VFX working branches. A review patch including both ARC migration
steps is saved at `/tmp/arc-flynn-output-budget-migration.patch`. No paid inference
was run.
