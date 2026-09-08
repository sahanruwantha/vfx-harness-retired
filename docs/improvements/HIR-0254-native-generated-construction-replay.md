---
id: HIR-0254
title: Generated construction must share the canonical unit identity
status: accepted
introduced_in: unreleased
date: 2026-09-08
failure_class: generated_candidate_loses_construction_dependency
mechanism: shared_artifact_paths_and_guarded_native_construction_preparation
adr: ADR-0012
---

# Generated construction must share the canonical unit identity

## Observed failure

The native generated-unit integration fixture promoted a real GLB, wrote a candidate
using `bvfx_import_construction()`, then failed both probe and canonical replay with
`bvfx_import_construction has no pin`. No completion was published. A disposable
confined-worker probe independently pinned and imported the same kind of GLB, so the
worker transport itself was not the cause. The failing fixture is retained in
`test_flynn_generated_unit.py`; diagnostic traces were captured in
`/tmp/vfx-generated-pin-test.log` during development.

## Root cause

Two path derivations disagreed. Script identity normalizes numeric layer `1` to
`build/units/01/<unit>.py`, while the construction publisher independently wrote
`build/units/1/<unit>.construction.json`. Separately, prepared candidate replay looked
for construction beside the physical scratch script, despite already carrying the
canonical script locator. Both paths lost the asset dependency before Blender.

## Mechanism and ownership

The pure `unit_artifact_paths` leaf owns script path derivation. Work-unit parsing
and construction pointers use it, avoiding a construction/work-units import cycle.
Prepared replay reads script bytes from the exact supplied source and construction
from its canonical locator. The existing descriptor-bound pointer and GLB records
remain in the evaluation and completion source closure.

The native builder records harness-selected construction preparation as a scripted
Flynn operation in the same SQLite run before model execution. Existing VFX staging,
promotion and witness verification remain authoritative. The operation has no model
usage and consumes operation/external capacity. The model cannot choose a generator
or asset; it receives the selected digest and pinned-import instruction in bounded
context. Current pointer, GLB and witness bindings are rechecked through dispatch,
capture, freeze and publication. Failed or uncertain preparation never falls back to
the legacy builder and never grants acceptance.

Preparation also requires sufficient remaining call capacity for the minimum complete
path, including required image captures and payments. It cannot consume the last
available operation on an asset when candidate execution and canonical replay would
then be impossible.

## Rejected alternatives

- Pinning only the live scene: cold replay would still have no asset dependency.
- Copying a pointer beside a scratch file: temporary output would become a competing
  construction authority instead of selecting the canonical unit identity.
- Guessing both numeric directory forms: obsolete paths would remain implicit
  authority and new publishers could continue to disagree.
- Moving generation policy into Flynn: generator selection, witness authority and
  asset promotion are VFX responsibilities. Flynn supplies guarded execution and
  durable accounting, which it already supports.

## Validation

The real-GLB native path now reaches canonical completion and cold image capture;
changing the asset afterward invalidates completion. Generation failure, staged-byte
cancellation and pointer, GLB or witness substitution refuse publication. Insufficient
capacity refuses before generation. Shared-path and existing-construction regressions
passed, along with the full frozen-source suite: **3,588 tests**, 121 Pillow
deprecation warnings, in `/tmp/vfx-spike-regression-8bo7fjfh`. Complete-source Ruff
and public CLI loading passed.
External generation and plate judgments in these tests are fixture adapters; they
do not establish live generation quality or qualitative qualification.

## Release and rollback

Ship with the native generated-construction route and its source bindings as one
change. There is no fallback lookup for an incorrectly placed old pointer. Existing
canonical artifacts continue to use their exact recorded dependencies; preparation
requires a current canonical pointer, and never silently adopts an obsolete one.

## Remaining limitations

Native eligibility still requires executable claims at every judge point and no
provisional visual requirements. Qualitative builder migration, explicit qualified
layer/debt/acceptance judgment and live end-to-end validation remain separate gates.
The closed but unwired retrieve route gains no implementation from this change.
