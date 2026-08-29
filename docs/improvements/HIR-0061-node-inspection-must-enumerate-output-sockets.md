---
id: HIR-0061
title: Node inspection must enumerate output sockets
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: missing_node_output_introspection
mechanism: typed_node_output_socket_inventory
adr: null
---

# Node inspection must enumerate output sockets

## Observed failure

In run `20260827T192500Z-abdc37`, `lighting_atmosphere` needed to wire Blender 5's Render
Layers node into Vector Blur. `inspect_nodes('compositor')` showed Vector Blur input sockets but
did not enumerate Render Layers outputs. The builder guessed `Z`, received a `KeyError`, attempted
a read-only `run_bpy` enumeration that the mutation guard correctly blocked, and had to keep
guessing despite having called the prescribed introspection tool.

## Root cause

The node report printed values for unlinked inputs and existing links only. Output sockets have no
default values, so an unlinked producer such as Render Layers exposed none of its callable API.
The tool's own error guidance said to inspect sockets, but the instrument omitted the needed side.

## Decision criteria

The read must be judgment-free, version-local, and complete enough to wire a graph without
spending mutation authority. It must not import Blender into unit tests or depend on node-type
special cases.

## General mechanism

`inspect_nodes` now enumerates every output socket name for every node in addition to unlinked
input values and links. Formatting lives in a pure duck-typed module used by the Blender worker,
so the exact report can be tested without `bpy`.

## Rejected patch-level alternatives

Adding `Z`/`Depth` advice to a prompt or recipe would encode one Blender-version guess. Allowing
read-only `run_bpy` probes would weaken the journal boundary. Special-casing Render Layers would
leave the same defect on other producer nodes.

## Validation

`test_node_report_enumerates_render_layer_outputs_without_mutation_probe` pins output names,
input values, and link formatting. Focused Ruff and pytest pass.

## Release and rollback

No data migration. Revert the pure formatter and worker call together if report size becomes a
measured issue; retain a complete typed output inventory through a more compact representation.

## Remaining limitations

The report names sockets but does not enumerate every RNA property. Helpers should own recurring
mechanical graph construction where a stable abstraction is possible.
