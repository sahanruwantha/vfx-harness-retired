---
id: HIR-0126
title: Unit replay is an identity-derived file
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: fragment_script_span_became_a_literal_filename
mechanism: identity_derived_unit_script_path
adr: null
---

# Unit replay is an identity-derived file

## Observed failure

A work unit's `mutates.script_spans` accepted any distinct relative path. Layer scripts
(`build/04_lighting.py`), `#fragment` addresses (`build/02_polish.py#polish`), and
alternate basenames (`build/units/02/not-polish.py`) were legal as long as they were
unique and did not collide with the composed layer `script`. Replay and builder
publication treat those strings as filesystem paths. A fragment token therefore becomes
a literal filename rather than an address inside a composed artifact, and a unit can
publish under a name that is not its identity.

The prior loader only required "exactly one span" and "distinct spans on a multi-unit
layer." Distinctness does not make a fragment a file.

## Root cause

Script authority was a free-form path list. Unit identity (`layer_id`, `unit.id`) and
the replay artifact were independent strings. The staging ticket enumerated
`script_spans` as unconstrained strings, so the materializer discovered the path rule
through later filesystem or digest failures instead of at the ticket boundary.

## Decision criteria

- Each unit owns exactly one replayable Python file:
  `build/units/<zero-padded-layer-id>/<unit-id>.py`.
- Composed layer scripts, `#fragment` notation, sibling directories, and alternate
  basenames are not script authority.
- The staging JSON schema enumerates that directory before generation. Staging and
  `load_layers` refuse any other span before bytes publish.
- Rematerialization may skip the check on the layer being replaced so a prior-generation
  DAG can still be read as the `apply_replan` base; every other layer still fails closed.
- Untracked production shots used as fixtures are adapted in temporary read-only views.
  The production loader is not weakened.

## General mechanism

`canonical_unit_script_path` / `validate_unit_script_path` are the single source.
`work_unit_authoring_schema(layer_id=...)` exposes the exact directory pattern on the
staging tool. `_validate_local_staged_units` and `load_layers` call the same validator.
`load_layers(..., replacing_layer_id=)` is the rematerialize exception only.

## Rejected patch-level alternatives

- Teaching the planner to write `build/units/...` in prose. Non-model callers and
  historical overlays would keep publishing fragments.
- Interpreting `#fragment` as a span inside the layer script. Replay is empty-scene
  concatenation of unit files, not addressable slices of a composed script.
- Silently rewriting illegal spans to the canonical path. That would launder authored
  identity and hide digest changes.
- Exempting untracked shot folders from `load_layers`. Compatibility belongs in test
  adapters, not in the production loader.

## Validation

`test_unit_ticket_schema_exposes_active_layer_script_directory` pins the authoring
schema. `test_unit_staging_refuses_noncanonical_replay_path_before_write` refuses
fragments, layer scripts, and wrong basenames without writing the candidate.
`test_layer_loader_rejects_fragment_script_authority` and
`test_layer_loader_rejects_composed_layer_script_as_unit_authority` pin the loader.

## Release and rollback

No persisted schema version bump. Newly staged units must use the identity-derived
path; selected views that still carry fragments fail closed until rematerialized.
Rollback would restore free-form spans and the fragment-as-filename defect.

## Remaining limitations

The canonical path does not create the file. Builders still journal into that artifact
after a unit is staged. Rematerialize still has to design a replacement DAG whose
spans match; skipping validation on the replaced layer is only so the old identity can
be read, not so it can publish again.
