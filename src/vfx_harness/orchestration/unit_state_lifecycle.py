"""Closed lifecycle edges shared by work-unit state transactions."""

from __future__ import annotations

TRANSITIONS = {
    "pending": {"planning", "blocked", "superseded"},
    "planning": {"building", "blocked", "failed", "retryable", "superseded"},
    "building": {
        "frozen",
        "blocked",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "superseded",
    },
    "frozen": {
        "evaluating",
        "building",
        "blocked",
        "failed",
        "retryable",
        "superseded",
    },
    "evaluating": {
        "passed",
        "repairing",
        "blocked",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "superseded",
    },
    "repairing": {
        "building",
        "frozen",
        "blocked",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "superseded",
    },
    "retryable": {"planning", "building", "blocked", "failed", "superseded"},
    "blocked": {"pending", "planning", "superseded"},
    "failed": {"retryable", "superseded"},
    "hypothesis_falsified": {"superseded"},
    "passed": {"superseded"},
    "superseded": set(),
}
