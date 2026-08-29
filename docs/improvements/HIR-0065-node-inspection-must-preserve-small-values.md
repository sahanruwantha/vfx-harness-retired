---
id: HIR-0065
title: Node inspection must preserve small values
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: small_calibration_rounded_to_zero
mechanism: significant_digit_socket_reporting
adr: null
---

# Node inspection must preserve small values

## Observed failure

The retry of `lighting_atmosphere` in run `20260827T203137Z-31b11f` warm-started a
deterministic script whose World Principled Volume density was calibrated to `1e-05` after an
earlier sweep proved larger values extinguished the long camera ray. `inspect_nodes` rounded the
socket to three decimals and reported `Density=0.0`. The builder inferred that the volume was
fake, replaced it with a `0.03..0.11` density field, and both retained image payments regressed to
an entirely black frame. The gates prevented sealing, but the false observation still consumed a
new tuning loop.

## Root cause

The typed report used fixed three-decimal rounding for every numeric socket. That presentation is
lossy for physically meaningful values whose scale is below `0.001`; zero and a small calibrated
nonzero value became observationally identical.

## General mechanism

Node input values now use six significant digits, including scientific notation where useful.
Vectors use the same scalar formatter per component. The report stays compact while preserving
the distinction between zero and small nonzero values such as `1e-05`.

## Rejected patch-level alternatives

Special-casing volume density would make the same defect recur for shader epsilon values,
compositor thresholds, or simulation controls. Adding prose that says the density is nonzero
would create a second authority and still contradict the typed read.

## Validation

`test_node_report_preserves_small_nonzero_socket_values` pins `1e-05`, ordinary decimals, and
vector formatting. The producing validation is a fresh warm-start build in which
`inspect_nodes(target="world")` exposes the calibrated density before any mutation.

## Release and rollback

This changes observation text only; no persisted schema or artifact migration is required.
Revert only with a replacement formatter that keeps zero distinguishable from every meaningful
finite nonzero value.

## Remaining limitations

Six significant digits are diagnostic precision, not serialized execution authority. Exact
contract evaluation continues to read Blender values directly.
