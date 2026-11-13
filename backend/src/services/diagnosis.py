"""Leave-one-out candidate diagnosis over an already frozen, completed run.

The original run is read-only evidence here. For every excluded candidate we
recompute the same vector-normalized weighted TOPSIS on the remaining samples
using the *exact* weight scenarios stored by the run (the same batch, never
re-drawn). Removing a row changes the L2 column norms and the positive/
negative ideal points, so remaining samples can swap order.

Pairwise ordering changes are split into two disjoint kinds:

* strict inversion  : a strict order (a>b or a<b) flips to the opposite
                      strict order;
* tie change        : any transition that involves an exact computed-score
                      tie on one side (tie<->strict or tie<->different tie).

Both the center weights and every Dirichlet scenario are compared, so each
pair carries scenario frequencies and a separate center-weight verdict.

The function is resumable: callers persist the per-excluded-candidate block
after each step and pass previously finished blocks back in; nothing is
recomputed for an excluded candidate that already has a block.
"""
import hashlib
import json

import numpy as np

from src.services.normalization import normalize_matrix, normalize_weights
from src.services.topsis import score_normalized

# How many reproducible counterexamples of each kind are kept per pair in
# the database result. The artifact always keeps the same digest subset; a
# pair with more changes simply records its full counts here.
EXAMPLES_PER_KIND = 3


def average_ranks_batch(scores):
    """Average (competition) ranks and tied-first shares, vectorized.

    Mirrors topsis.average_ranks exactly: rows that are equal in computed
    closeness receive the mean of their rank positions and split one
    first-place share. ``scores`` has shape (rows, candidates).
    """
    s = np.asarray(scores, dtype=float)
    rows, n = s.shape
    order = np.argsort(-s, axis=1, kind="stable")
    sorted_s = np.take_along_axis(s, order, axis=1)
    ranks = np.empty((rows, n), dtype=float)
    first = np.zeros((rows, n), dtype=float)
    for r in range(rows):
        row_order = order[r]
        row_sorted = sorted_s[r]
        start = 0
        while start < n:
            end = start + 1
            while end < n and row_sorted[end] == row_sorted[start]:
                end += 1
            avg = (start + 1 + end) / 2.0
            ranks[r, row_order[start:end]] = avg
            if start == 0:
                first[r, row_order[start:end]] = 1.0 / (end - start)
            start = end
    return ranks, first


def score_batch(normalized, directions, weights_batch):
    """TOPSIS closeness for many weight rows on one normalized matrix.

    Arithmetic follows topsis.score_normalized operation by operation
    (column max/min ideals, stable scaled Euclidean distances, the same
    divide-with-masking and the .5 fallback) so batch and per-scenario
    results are bit-identical. Returns scores and rank/first-share arrays.
    """
    x = np.asarray(normalized, dtype=float)
    n, m = x.shape
    w = np.asarray(weights_batch, dtype=float)
    if w.ndim == 1:
        w = w[None, :]
    if w.shape[1] != m:
        raise ValueError("权重维数错误")
    # Normalize each weight row exactly like normalize_weights (peak scaling).
    peak = np.max(w, axis=1, keepdims=True)
    if np.any(peak == 0) or not np.isfinite(w).all() or (w < 0).any():
        raise ValueError("情景权重必须非负且权重和为正")
    w = w / peak
    w = w / np.sum(w, axis=1, keepdims=True)

    benefit = np.array([d == "benefit" for d in directions])
    bmask = benefit[None, :]  # (1, criteria), broadcasts over scenarios
    # v shape: (scenarios, candidates, criteria)
    v = w[:, None, :] * x[None, :, :]
    # Reduce over real values first, then select by direction: mirrors the
    # scalar np.where(benefit, max(v), min(v)) and keeps all-zero columns
    # (zero scenario weight) at 0 instead of reducing infinities.
    col_max = np.max(v, axis=1)
    col_min = np.min(v, axis=1)
    best = np.where(bmask, col_max, col_min)
    worst = np.where(bmask, col_min, col_max)
    diff_best = v - best[:, None, :]
    diff_worst = v - worst[:, None, :]
    dp = _stable_norm_batch(diff_best)
    dm = _stable_norm_batch(diff_worst)

    scale = np.maximum(dp, dm)
    a = np.divide(dm, scale, out=np.zeros_like(dm), where=scale != 0)
    b = np.divide(dp, scale, out=np.zeros_like(dp), where=scale != 0)
    scores = np.divide(
        a, a + b, out=np.full_like(a, 0.5), where=scale != 0
    )
    ranks, first = average_ranks_batch(scores)
    return scores, ranks, first


def _stable_norm_batch(delta):
    """Stable scaled Euclidean norm over the last axis, batch version."""
    scale = np.max(np.abs(delta), axis=2, keepdims=True)
    # Explicit zero mask: an all-zero distance column must stay 0 instead
    # of producing 0/0 -> NaN (happens when a scenario weight is 0).
    scaled = np.where(scale != 0, delta / np.where(scale == 0, 1.0, scale), 0.0)
    return np.squeeze(scale, axis=2) * np.sqrt(
        np.sum(scaled * scaled, axis=2)
    )


def _relation(sign):
    if sign > 0:
        return "greater"
    if sign < 0:
        return "less"
    return "tie"


def _transition(base_sign, new_sign):
    """Classify one pairwise comparison before vs after exclusion."""
    if base_sign == new_sign:
        return "unchanged"
    if base_sign != 0 and new_sign != 0:
        return "strict_inversion"
    return "tie_change"


def _codes(snapshot):
    samples = sorted(snapshot["samples"], key=lambda x: x["code"])
    criteria = sorted(snapshot["criteria"], key=lambda x: x["code"])
    return samples, criteria


def _matrix(samples, codes):
    return np.array(
        [[s[c] for c in codes] for s in samples], dtype=float
    )


def _pair_index(n):
    return [(i, j) for i in range(n) for j in range(i + 1, n)]


def prepare_context(run):
    """Freeze everything shared by all leave-one-out blocks.

    The original run is only read. The stored Dirichlet weight scenarios are
    reused verbatim (same batch and order), and a baseline re-computation
    checks the batch scorer reproduces every archived scenario score.
    """
    snapshot = run["snapshot"] if isinstance(run, dict) else run.snapshot
    result = run["result"] if isinstance(run, dict) else run.result
    center_weights = (
        run["center_weights"] if isinstance(run, dict) else run.center_weights
    )
    samples, criteria = _codes(snapshot)
    codes = [c["code"] for c in criteria]
    directions = [c["direction"] for c in criteria]
    n = len(samples)
    if n < 3:
        raise ValueError("候选少于3份时，逐一剔除不足以观察剩余试样的两两逆序")
    sample_codes = [s["code"] for s in samples]
    matrix = _matrix(samples, codes)
    normalized, columns = normalize_matrix(matrix)
    center = normalize_weights(
        [center_weights[c] for c in codes], len(codes), True
    )

    # Reuse the exact stored scenario weights: same batch, same order.
    draws = result.get("draws") or []
    repeats = len(draws)
    if not repeats:
        raise ValueError("原权衡未保留权重情景，无法沿用同一批情景诊断")
    scenario_weights = np.array(
        [d["weights"] for d in draws], dtype=float
    )
    base_scores, base_ranks, _ = score_batch(
        normalized, directions, scenario_weights
    )
    base_center = score_normalized(normalized, directions, center)
    base_center_scores = base_center["scores"]
    base_ideal_best = base_center["ideal_best"]
    base_ideal_worst = base_center["ideal_worst"]
    base_norms = np.array([col["norm"] for col in columns], dtype=float)

    # Independent re-computation check: the batch scorer on the stored
    # scenarios must reproduce the scores the original run archived.
    stored_scores = np.array(
        [d["scores"] for d in draws], dtype=float
    )
    reproducible = bool(np.array_equal(base_scores, stored_scores))
    max_score_gap = float(np.max(np.abs(base_scores - stored_scores)))

    return {
        "run": run,
        "result": result,
        "samples": samples,
        "criteria": criteria,
        "codes": codes,
        "directions": directions,
        "n": n,
        "sample_codes": sample_codes,
        "matrix": matrix,
        "center": center,
        "draws": draws,
        "repeats": repeats,
        "scenario_weights": scenario_weights,
        "base_scores": base_scores,
        "base_ranks": base_ranks,
        "base_center_scores": base_center_scores,
        "base_ideal_best": base_ideal_best,
        "base_ideal_worst": base_ideal_worst,
        "base_norms": base_norms,
        "pairs": _pair_index(n),
        "baseline_recheck": {
            "stored_scenarios_match_recompute": reproducible,
            "max_abs_score_gap": max_score_gap,
            "rule": "batch TOPSIS on stored scenario weights must equal the archived scores exactly",
        },
    }


def build_block(ctx, excluded_idx):
    """Compute the single leave-one-out block for one excluded candidate."""
    samples = ctx["samples"]
    n = ctx["n"]
    pairs = ctx["pairs"]
    sample_codes = ctx["sample_codes"]
    directions = ctx["directions"]
    matrix = ctx["matrix"]
    base_norms = ctx["base_norms"]
    base_ideal_best = ctx["base_ideal_best"]
    base_ideal_worst = ctx["base_ideal_worst"]
    base_center_scores = ctx["base_center_scores"]
    base_scores = ctx["base_scores"]
    base_ranks = ctx["base_ranks"]
    repeats = ctx["repeats"]
    draws = ctx["draws"]
    scenario_weights = ctx["scenario_weights"]
    center = ctx["center"]

    excluded = samples[excluded_idx]
    keep = [k for k in range(n) if k != excluded_idx]
    keep_pos = {k: pos for pos, k in enumerate(keep)}
    reduced_samples = [samples[k] for k in keep]
    reduced_matrix = matrix[keep, :]
    reduced_norm, reduced_columns = normalize_matrix(reduced_matrix)
    reduced_center = score_normalized(
        reduced_norm, directions, center
    )
    reduced_scores, reduced_ranks, _ = score_batch(
        reduced_norm, directions, scenario_weights
    )

    norm_after = np.array(
        [col["norm"] for col in reduced_columns], dtype=float
    )
    norm_delta = norm_after - base_norms
    ideal_best_delta = reduced_center["ideal_best"] - base_ideal_best
    ideal_worst_delta = (
        reduced_center["ideal_worst"] - base_ideal_worst
    )

    # Per-pair aggregation over the remaining samples. Indices refer to
    # the original (full-set) sample ordering. Sign/transition logic is
    # vectorized over scenarios; only small counterexample digests are built.
    base_diff_sign = np.sign(
        base_center_scores[None, :] - base_center_scores[:, None]
    ).astype(np.int8)
    reduced_center_sign = np.sign(
        reduced_center["scores"][None, :]
        - reduced_center["scores"][:, None]
    ).astype(np.int8)

    pair_records = []
    strict_total = tie_total = 0
    center_strict_total = center_tie_total = 0
    for left, right in pairs:
        if excluded_idx in (left, right):
            continue
        li = keep_pos[left]
        ri = keep_pos[right]
        base_sign = np.sign(
            base_scores[:, left] - base_scores[:, right]
        ).astype(np.int8)
        new_sign = np.sign(
            reduced_scores[:, li] - reduced_scores[:, ri]
        ).astype(np.int8)
        strict_mask = (base_sign != 0) & (new_sign != 0) & (
            base_sign != new_sign
        )
        tie_mask = ((base_sign == 0) | (new_sign == 0)) & (
            base_sign != new_sign
        )
        strict_scenarios = np.nonzero(strict_mask)[0]
        tie_scenarios = np.nonzero(tie_mask)[0]
        strict_total += len(strict_scenarios)
        tie_total += len(tie_scenarios)

        center_base_sign = int(base_diff_sign[right, left])
        center_new_sign = int(reduced_center_sign[ri, li])
        center_kind = _transition(
            center_base_sign, center_new_sign
        )
        if center_kind == "strict_inversion":
            center_strict_total += 1
        elif center_kind == "tie_change":
            center_tie_total += 1

        record = {
            "pair": [sample_codes[left], sample_codes[right]],
            "scenario_comparisons": int(repeats),
            "strict_inversion_count": int(len(strict_scenarios)),
            "tie_change_count": int(len(tie_scenarios)),
            "strict_inversion_frequency": float(
                len(strict_scenarios) / repeats
            ),
            "tie_change_frequency": float(
                len(tie_scenarios) / repeats
            ),
            "center_relation_before": _relation(center_base_sign),
            "center_relation_after": _relation(center_new_sign),
            "center_change": center_kind,
            "examples": _examples(
                strict_scenarios,
                tie_scenarios,
                draws,
                scenario_weights,
                base_scores,
                base_ranks,
                reduced_scores,
                reduced_ranks,
                reduced_samples,
                sample_codes,
                left,
                right,
                li,
                ri,
                keep,
            ),
        }
        pair_records.append(record)

    return {
        "excluded_code": excluded["code"],
        "excluded_id": excluded["id"],
        "excluded_name": excluded["name"],
        "remaining_count": n - 1,
        "norms_before": base_norms.tolist(),
        "norms_after": norm_after.tolist(),
        "norm_delta": norm_delta.tolist(),
        "ideal_best_before": base_ideal_best.tolist(),
        "ideal_best_after": reduced_center["ideal_best"].tolist(),
        "ideal_best_delta": ideal_best_delta.tolist(),
        "ideal_worst_before": base_ideal_worst.tolist(),
        "ideal_worst_after": reduced_center["ideal_worst"].tolist(),
        "ideal_worst_delta": ideal_worst_delta.tolist(),
        "strict_inversion_events": int(strict_total),
        "tie_change_events": int(tie_total),
        "center_strict_inversion_pairs": int(center_strict_total),
        "center_tie_change_pairs": int(center_tie_total),
        "pairs": pair_records,
    }


def block_header(block):
    """Constant-size block summary for the main diagnosis row.

    Pair-level records stay in the checkpoint child table (and, with full
    evidence, in the per-exclusion artifact files), so the main row never grows
    with the O(n^2) number of candidate pairs even at the 120-candidate limit.
    """
    return {
        "excluded_code": block["excluded_code"],
        "excluded_id": block["excluded_id"],
        "excluded_name": block["excluded_name"],
        "remaining_count": block["remaining_count"],
        "norms_before": block["norms_before"],
        "norms_after": block["norms_after"],
        "norm_delta": block["norm_delta"],
        "ideal_best_before": block["ideal_best_before"],
        "ideal_best_after": block["ideal_best_after"],
        "ideal_best_delta": block["ideal_best_delta"],
        "ideal_worst_before": block["ideal_worst_before"],
        "ideal_worst_after": block["ideal_worst_after"],
        "ideal_worst_delta": block["ideal_worst_delta"],
        "strict_inversion_events": block["strict_inversion_events"],
        "tie_change_events": block["tie_change_events"],
        "center_strict_inversion_pairs": block[
            "center_strict_inversion_pairs"
        ],
        "center_tie_change_pairs": block["center_tie_change_pairs"],
        "changed_pair_count": sum(
            1
            for rec in block["pairs"]
            if rec["strict_inversion_count"]
            or rec["tie_change_count"]
            or rec["center_change"] != "unchanged"
        ),
    }


def slim_block(block):
    """Project a full block to its database-safe form.

    Counterexample digests can be large and live in the artifact; the DB keeps
    counts, center-weight verdicts and norm/ideal-point changes so progress,
    resume and the page do not depend on reading the artifact file.
    """
    return {
        "excluded_code": block["excluded_code"],
        "excluded_id": block["excluded_id"],
        "excluded_name": block["excluded_name"],
        "remaining_count": block["remaining_count"],
        "norms_before": block["norms_before"],
        "norms_after": block["norms_after"],
        "norm_delta": block["norm_delta"],
        "ideal_best_before": block["ideal_best_before"],
        "ideal_best_after": block["ideal_best_after"],
        "ideal_best_delta": block["ideal_best_delta"],
        "ideal_worst_before": block["ideal_worst_before"],
        "ideal_worst_after": block["ideal_worst_after"],
        "ideal_worst_delta": block["ideal_worst_delta"],
        "strict_inversion_events": block["strict_inversion_events"],
        "tie_change_events": block["tie_change_events"],
        "center_strict_inversion_pairs": block[
            "center_strict_inversion_pairs"
        ],
        "center_tie_change_pairs": block["center_tie_change_pairs"],
        "pairs": [
            {
                "pair": rec["pair"],
                "scenario_comparisons": rec["scenario_comparisons"],
                "strict_inversion_count": rec["strict_inversion_count"],
                "tie_change_count": rec["tie_change_count"],
                "strict_inversion_frequency": rec[
                    "strict_inversion_frequency"
                ],
                "tie_change_frequency": rec["tie_change_frequency"],
                "center_relation_before": rec["center_relation_before"],
                "center_relation_after": rec["center_relation_after"],
                "center_change": rec["center_change"],
            }
            for rec in block["pairs"]
        ],
    }


def _document_header(ctx, aggregate):
    result = ctx["result"]
    n = ctx["n"]
    repeats = ctx["repeats"]
    sample_codes = ctx["sample_codes"]
    codes = ctx["codes"]
    document = {
        "version": "leave-one-out-diagnosis-v1",
        "run_id": _run_id(ctx["run"]),
        "run_fingerprint": result.get("fingerprint", ""),
        "excluded_candidates": n,
        "remaining_per_diagnosis": n - 1,
        "scenarios_reused": repeats,
        "scenario_seed": result.get("seed"),
        "criteria_codes": codes,
        "sample_codes": sample_codes,
        "baseline_recheck": ctx["baseline_recheck"],
        "rank_rule": "exact equal computed closeness scores receive average ranks; only computed-score ties count as ties",
        "change_rule": "strict_inversion flips a strict order; tie_change is any transition involving a tie; the two kinds are disjoint",
        "aggregate": aggregate,
    }
    canonical = {
        "version": document["version"],
        "run_fingerprint": document["run_fingerprint"],
        "scenarios_reused": repeats,
        "sample_codes": sample_codes,
        "criteria_codes": codes,
    }
    document["diagnosis_fingerprint"] = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return document


def build_manifest(ctx, blocks):
    """Small manifest: metadata, aggregate and constant-size block headers.

    Pair-level detail and counterexamples live in per-exclusion block files, so
    the manifest stays small even at the 120-candidate limit.
    """
    n = ctx["n"]
    repeats = ctx["repeats"]
    sample_codes = ctx["sample_codes"]
    blocks = sorted(
        blocks, key=lambda b: sample_codes.index(b["excluded_code"])
    )
    aggregate = _aggregate(blocks, repeats, n)
    document = _document_header(ctx, aggregate)
    document["block_files"] = [
        f"blocks/{b['excluded_code']}.json" for b in blocks
    ]
    document["blocks"] = [block_header(b) for b in blocks]
    return document


def finalize_document(ctx, blocks):
    """Assemble a full in-memory document (all blocks with evidence).

    Used by one-shot callers and tests; the worker persists blocks individually
    via :func:`build_manifest` plus per-exclusion files instead of writing one
    large document at the 120-candidate limit.
    """
    n = ctx["n"]
    repeats = ctx["repeats"]
    sample_codes = ctx["sample_codes"]
    blocks = sorted(
        blocks, key=lambda b: sample_codes.index(b["excluded_code"])
    )
    aggregate = _aggregate(blocks, repeats, n)
    document = _document_header(ctx, aggregate)
    document["blocks"] = blocks
    return document


def build_diagnosis(run, progress=None, finished_blocks=None):
    """Compute every leave-one-out block for a completed DecisionRun.

    Thin wrapper over :func:`prepare_context`, :func:`build_block` and
    :func:`finalize_document` kept for one-shot callers and tests.
    ``finished_blocks`` maps an excluded sample code to a previously built
    block so callers can resume without recomputation.
    """
    finished_blocks = finished_blocks or {}
    ctx = prepare_context(run)
    blocks = []
    total = ctx["n"]
    done = 0
    for excluded_idx, excluded in enumerate(ctx["samples"]):
        if excluded["code"] in finished_blocks:
            blocks.append(finished_blocks[excluded["code"]])
            done += 1
            if progress:
                progress(done, total, excluded["code"], resumed=True)
            continue
        block = build_block(ctx, excluded_idx)
        blocks.append(block)
        done += 1
        if progress:
            progress(done, total, excluded["code"], resumed=False)
    return finalize_document(ctx, blocks)


def _run_id(run):
    if isinstance(run, dict):
        return run.get("id")
    return getattr(run, "id", None)


def _examples(
    strict_scenarios,
    tie_scenarios,
    draws,
    scenario_weights,
    base_scores,
    base_ranks,
    reduced_scores,
    reduced_ranks,
    reduced_samples,
    sample_codes,
    left,
    right,
    li,
    ri,
    keep,
):
    """Build reproducible counterexamples (capped per kind)."""

    def one(kind, idx):
        draw = draws[idx]
        return {
            "kind": kind,
            "scenario_repeat": int(draw["repeat"]),
            "scenario_index": int(idx),
            "weights": [float(x) for x in scenario_weights[idx]],
            "left": {
                "code": sample_codes[left],
                "score_before": float(base_scores[idx, left]),
                "rank_before": float(base_ranks[idx, left]),
                "score_after": float(reduced_scores[idx, li]),
                "rank_after": float(reduced_ranks[idx, li]),
            },
            "right": {
                "code": sample_codes[right],
                "score_before": float(base_scores[idx, right]),
                "rank_before": float(base_ranks[idx, right]),
                "score_after": float(reduced_scores[idx, ri]),
                "rank_after": float(reduced_ranks[idx, ri]),
            },
            "relation_before": _relation(
                int(
                    np.sign(
                        base_scores[idx, left] - base_scores[idx, right]
                    )
                )
            ),
            "relation_after": _relation(
                int(
                    np.sign(
                        reduced_scores[idx, li] - reduced_scores[idx, ri]
                    )
                )
            ),
        }

    strict = [
        one("strict_inversion", int(i))
        for i in strict_scenarios[:EXAMPLES_PER_KIND]
    ]
    tie = [
        one("tie_change", int(i))
        for i in tie_scenarios[:EXAMPLES_PER_KIND]
    ]
    return {"strict_inversion": strict, "tie_change": tie}


def _aggregate(blocks, repeats, n):
    # Sum event counts across all exclusions that kept both pair members.
    strict_by_pair: dict[tuple, int] = {}
    tie_by_pair: dict[tuple, int] = {}
    observed_by_pair: dict[tuple, int] = {}
    total_strict = total_tie = 0
    center_strict = center_tie = 0
    for block in blocks:
        total_strict += block["strict_inversion_events"]
        total_tie += block["tie_change_events"]
        center_strict += block["center_strict_inversion_pairs"]
        center_tie += block["center_tie_change_pairs"]
        for rec in block["pairs"]:
            key = tuple(rec["pair"])
            observed_by_pair[key] = observed_by_pair.get(key, 0) + 1
            strict_by_pair[key] = (
                strict_by_pair.get(key, 0) + rec["strict_inversion_count"]
            )
            tie_by_pair[key] = (
                tie_by_pair.get(key, 0) + rec["tie_change_count"]
            )

    # Each remaining pair is observed under (n-2) exclusions x repeats.
    pair_summary = []
    for key, obs in observed_by_pair.items():
        comparisons = obs * repeats
        pair_summary.append(
            {
                "pair": list(key),
                "exclusions_observed": obs,
                "scenario_comparisons": comparisons,
                "strict_inversion_events": strict_by_pair[key],
                "tie_change_events": tie_by_pair[key],
                "strict_inversion_frequency": (
                    strict_by_pair[key] / comparisons if comparisons else 0.0
                ),
                "tie_change_frequency": (
                    tie_by_pair[key] / comparisons if comparisons else 0.0
                ),
            }
        )
    pair_summary.sort(
        key=lambda r: (
            -r["strict_inversion_frequency"],
            -r["tie_change_frequency"],
            r["pair"],
        )
    )
    most_influential = sorted(
        blocks,
        key=lambda b: (
            -b["strict_inversion_events"],
            -b["tie_change_events"],
            b["excluded_code"],
        ),
    )[0]["excluded_code"]
    return {
        "total_strict_inversion_events": total_strict,
        "total_tie_change_events": total_tie,
        "center_weight_strict_inversion_pairs": center_strict,
        "center_weight_tie_change_pairs": center_tie,
        "most_influential_excluded_code": most_influential,
        "pair_summary": pair_summary,
    }
