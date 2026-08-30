---
id: HIR-0149
title: Provisional debt preserves the authored proposition
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: materialization_rewrites_qualitative_requirement_as_meta_debt
mechanism: immutable_requirement_statement_in_domain_binding
adr: ADR-0006
---

# Provisional debt preserves the authored proposition

## Observed failure

After HIR-0145 rematerialized Room 1046 Layer 2, the selected map correctly retained
R51's `image + scene` domains. Its provisional image row, however, replaced the authored
visual proposition with: the debt is deferred to the first downstream look-dev/lighting
layer and is provisionally accepted here. Similar meta-statements replaced R6, R8, R9,
and R58.

Composed judgment consumes the provisional statement as its qualitative claim. It could
therefore be asked to confirm that deferral text exists rather than judge whether the
current Layer 2 form reads as the specific hotel. The domain survived, but its proposition
was laundered.

## Root cause

Materialization treated a provisional decision's `statement` and strength as equally
authorable. The requirement already has an immutable cited statement, so giving the JIT
agent another free-form proposition was duplicate authority. Durable parsing checked that
all provisional domain rows shared text, not that the text remained the requirement.

## Decision

- A provisional domain binding carries the exact authored requirement statement.
- Materialization may choose only `approved_start` or `planner_start`; it cannot paraphrase,
  narrow, defer, or replace the proposition.
- The staging schema enumerates the active layer's authored statements. Validation ties
  the selected statement to the exact requirement id and names expected versus found.
- Publication writes the canonical top-level statement into every provisional domain row,
  never the candidate copy.
- The durable requirement parser rejects any provisional row whose statement differs from
  its top-level cited requirement.
- Defense in depth makes composed judgment read the sparse bundle's authored statement,
  not the selected binding's text. A malformed legacy/current view cannot change what is
  judged.
- Vocabulary-gap records remain audit context; their ids and meta-explanations do not
  replace the visual proposition.

## General mechanism

The materialization tool compiler loads the active layer's exact owned requirement map
from the selected immutable global bundle and exposes those statements as a closed enum.
The validator compares each decision with its base register row, then compiles the domain
binding from that base row. `load_requirements` enforces the same invariant for selected
authority. The composition adapter uses the original deferred-owner register statement
even when a selected row is malformed.

## Rejected alternatives

- Ask the critic to infer intent from meta-deferral prose: the wrong claim has already
  crossed the authority boundary.
- Require the authored statement only to be a substring: paraphrase and negation remain
  representable.
- Put the vocabulary-gap id into the decision statement: audit metadata is not authored
  intent.
- Fix only Room 1046's selected JSON: generated state is not an authority-edit surface.
- Make the prompt louder: free-form duplicate authority remains mechanically legal.

## Validation

Regression tests prove materialization rejects a downstream-deferral statement, the
selected requirement parser rejects a mismatched provisional row, and composed judgment
uses the authored proposition even when selected decision text is meta-debt. Existing
domain-AND, padding, provisional-falsification, staging, and plan-tool suites remain green.

## Release and rollback

No schema version change. Existing materialized views with rewritten provisional text
must rematerialize; composed judgment is immediately safe because it reads the authored
base statement. Rollback would restore duplicate intent authority and is unsafe.

