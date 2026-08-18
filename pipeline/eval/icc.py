"""ICC(2,1) — two-way random, single-measure, absolute agreement.

Phase 0.2 needs a between/within split, not just a one-pair standard deviation.
One subject rated n times cannot produce an ICC (no between-target variance). This
module is the crossed case: targets × raters, missing cells allowed.

Shrout & Fleiss ICC(2,1) = (BMS − EMS) / (BMS + (k−1) EMS + k (JMS − EMS) / n)
where BMS is between-targets, JMS between-raters, EMS residual.
"""

from __future__ import annotations


def icc_2_1(matrix: list[list[float | None]]) -> dict:
    """`matrix[target][rater]`. None is a missing cell.

    Returns the coefficient plus the mean squares so a small-n result cannot be
    quoted as if it were a large-n one.
    """
    if not matrix or not matrix[0]:
        return {"ok": False, "reason": "empty matrix", "icc": None}
    n = len(matrix)
    k = len(matrix[0])
    if any(len(row) != k for row in matrix):
        return {"ok": False, "reason": "ragged matrix", "icc": None}

    cells = [(i, j, matrix[i][j]) for i in range(n) for j in range(k)
             if isinstance(matrix[i][j], (int, float))]
    N = len(cells)
    if N < 4 or n < 2 or k < 2:
        return {"ok": False, "reason": f"need ≥2 targets and ≥2 raters (got n={n} k={k} N={N})",
                "icc": None, "n_targets": n, "n_raters": k, "n_cells": N}

    mean = sum(v for _i, _j, v in cells) / N
    row_n = [sum(1 for j in range(k) if isinstance(matrix[i][j], (int, float))) for i in range(n)]
    col_n = [sum(1 for i in range(n) if isinstance(matrix[i][j], (int, float))) for j in range(k)]
    if min(row_n) == 0 or min(col_n) == 0:
        return {"ok": False, "reason": "a target or rater has no scores", "icc": None}

    row_mean = [sum(matrix[i][j] for j in range(k)
                    if isinstance(matrix[i][j], (int, float))) / row_n[i] for i in range(n)]
    col_mean = [sum(matrix[i][j] for i in range(n)
                    if isinstance(matrix[i][j], (int, float))) / col_n[j] for j in range(k)]

    bms = k * sum((m - mean) ** 2 for m in row_mean) / (n - 1) if n > 1 else 0.0
    # For unbalanced missingness the classical formula is an approximation; we still
    # use the complete-design degrees of freedom and label it.
    jms = n * sum((m - mean) ** 2 for m in col_mean) / (k - 1) if k > 1 else 0.0
    ems = 0.0
    df_e = (n - 1) * (k - 1)
    if df_e > 0:
        ems = sum((v - row_mean[i] - col_mean[j] + mean) ** 2
                  for i, j, v in cells) / df_e

    denom = bms + (k - 1) * ems + k * (jms - ems) / n
    icc = (bms - ems) / denom if denom else None
    return {
        "ok": icc is not None,
        "icc": None if icc is None else round(icc, 3),
        "n_targets": n,
        "n_raters": k,
        "n_cells": N,
        "balanced": N == n * k,
        "BMS": round(bms, 4),
        "JMS": round(jms, 4),
        "EMS": round(ems, 4),
        "within": round(ems ** 0.5, 4),
        "between": round(bms ** 0.5, 4),
    }
