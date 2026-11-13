"""Numeric oracle tests for leave-one-out diagnosis (run with venv python).

Run from repo root:
  PYTHONPATH=backend /tmp/venv/bin/python test-artifacts/gb-0024/test_diagnosis.py
"""
import sys

import numpy as np

from src.services.normalization import normalize_matrix
from src.services.topsis import score_normalized, average_ranks
from src.services.weight_sensitivity import analyze_decision
from src.services.diagnosis import (
    score_batch,
    average_ranks_batch,
    build_diagnosis,
    build_manifest,
    block_header,
    _transition,
)

rng = np.random.default_rng(7)
fails = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        fails.append(name)


# 1. average_ranks_batch matches the scalar version, incl. exact ties.
for trial in range(200):
    rows = rng.integers(2, 30)
    cols = rng.integers(2, 12)
    s = rng.random((rows, cols))
    if trial % 5 == 0:  # force exact ties
        s = np.round(s, 1)
    br, bf = average_ranks_batch(s)
    for r in range(rows):
        er, ef = average_ranks(s[r])
        assert np.array_equal(br[r], er), (trial, r)
        assert np.array_equal(bf[r], ef), (trial, r)
check("average_ranks_batch == scalar average_ranks (200 random matrices)", True)

# 2. score_batch is bit-identical to score_normalized on random matrices.
for trial in range(60):
    n, m = rng.integers(3, 40), rng.integers(2, 6)
    matrix = rng.random((n, m)) * rng.integers(1, 1000)
    directions = [
        "benefit" if rng.random() > 0.4 else "cost" for _ in range(m)
    ]
    normalized, _ = normalize_matrix(matrix)
    k = rng.integers(5, 50)
    weights = rng.dirichlet(np.ones(m), size=k)
    weights[0, 0] = 0.0  # legal zero component
    bs, br, bf = score_batch(normalized, directions, weights)
    for i, w in enumerate(weights):
        ref = score_normalized(normalized, directions, w)
        if not np.array_equal(bs[i], ref["scores"]):
            check("score_batch bit-identical scores", False, f"trial {trial} i {i}")
            break
        if not np.array_equal(br[i], ref["ranks"]):
            check("score_batch bit-identical ranks", False, f"trial {trial}")
            break
        if not np.array_equal(bf[i], ref["first_share"]):
            check("score_batch bit-identical first share", False, f"trial {trial}")
            break
    else:
        continue
    break
else:
    check("score_batch bit-identical to score_normalized (60 trials, zero weight included)", True)

# 3. Transition classifier: strict vs tie are disjoint and cover the change.
cases = [
    (1, -1, "strict_inversion"),
    (-1, 1, "strict_inversion"),
    (1, 0, "tie_change"),
    (0, -1, "tie_change"),
    (0, 0, "unchanged"),
    (1, 1, "unchanged"),
]
for b, nw, expect in cases:
    check(f"transition {b}->{nw} == {expect}", _transition(b, nw) == expect)

# 4. End-to-end on a synthetic frozen run built via analyze_decision.
def make_snapshot(n=8, m=4, seed=11):
    r = np.random.default_rng(seed)
    samples = []
    raw = r.uniform(0.2, 100, size=(n, m))
    for i in range(n):
        row = {
            "id": i + 1,
            "code": f"S{i+1:02}",
            "name": f"sample {i+1}",
        }
        for j, key in enumerate(["a", "b", "c", "d"]):
            row[key] = float(raw[i, j])
        samples.append(row)
    criteria = [
        {"id": 1, "code": "a", "name": "A", "unit": "u",
         "direction": "benefit", "center_weight": 0.3},
        {"id": 2, "code": "b", "name": "B", "unit": "u",
         "direction": "benefit", "center_weight": 0.25},
        {"id": 3, "code": "c", "name": "C", "unit": "u",
         "direction": "cost", "center_weight": 0.25},
        {"id": 4, "code": "d", "name": "D", "unit": "u",
         "direction": "cost", "center_weight": 0.2},
    ]
    return {"samples": samples, "criteria": criteria}


snapshot = make_snapshot()
center = {c["code"]: c["center_weight"] for c in snapshot["criteria"]}
full = analyze_decision(
    snapshot, center, concentration=40, repeats=1000, seed=20261112
)
run = {
    "id": 99,
    "snapshot": snapshot,
    "center_weights": center,
    "result": full,
}
doc = build_diagnosis(run)
check(
    "baseline recheck reproduces archived scores exactly",
    doc["baseline_recheck"]["stored_scenarios_match_recompute"] is True
    and doc["baseline_recheck"]["max_abs_score_gap"] == 0.0,
    str(doc["baseline_recheck"]),
)
check("one block per excluded candidate", len(doc["blocks"]) == 8)
check("scenarios reused = 1000", doc["scenarios_reused"] == 1000)

# Every LOO recomputation uses n-1 rows; norm/ideal arrays length = m.
b0 = doc["blocks"][0]
check("remaining count 7", b0["remaining_count"] == 7)
check("norm vectors length 4", len(b0["norms_after"]) == 4)
check(
    "norm delta equals after-before",
    np.allclose(
        np.array(b0["norm_delta"]),
        np.array(b0["norms_after"]) - np.array(b0["norms_before"]),
    ),
)

# Recompute one block independently with the public scorer as an oracle.
excluded = 0
keep = [k for k in range(8) if k != excluded]
codes = [c["code"] for c in snapshot["criteria"]]
directions = [c["direction"] for c in snapshot["criteria"]]
matrix = np.array(
    [[s[c] for c in codes] for s in snapshot["samples"]], dtype=float
)
red_norm, red_cols = normalize_matrix(matrix[keep])
check(
    "block norms_after matches independent normalize",
    np.allclose(b0["norms_after"], [c["norm"] for c in red_cols]),
)
# Manually count changes for one pair with the public scorer.
base_norm, _ = normalize_matrix(matrix)
base_sc, _, _ = score_batch(base_norm, directions,
                            np.array([d["weights"] for d in full["draws"]]))
red_sc, _, _ = score_batch(red_norm, directions,
                           np.array([d["weights"] for d in full["draws"]]))
li, ri = keep.index(1), keep.index(2)
bsign = np.sign(base_sc[:, 1] - base_sc[:, 2]).astype(int)
rsign = np.sign(red_sc[:, li] - red_sc[:, ri]).astype(int)
exp_strict = int(np.sum([_transition(int(a), int(b)) == "strict_inversion"
                         for a, b in zip(bsign, rsign)]))
exp_tie = int(np.sum([_transition(int(a), int(b)) == "tie_change"
                      for a, b in zip(bsign, rsign)]))
rec = next(p for p in b0["pairs"] if p["pair"] == ["S02", "S03"])
check("manual strict count matches block", rec["strict_inversion_count"] == exp_strict,
      f"{rec['strict_inversion_count']} vs {exp_strict}")
check("manual tie count matches block", rec["tie_change_count"] == exp_tie,
      f"{rec['tie_change_count']} vs {exp_tie}")
check("strict + tie <= scenarios",
      rec["strict_inversion_count"] + rec["tie_change_count"] <= 1000)

# 5. Aggregate pair summary frequencies are consistent with block counts.
agg = doc["aggregate"]
for ps in agg["pair_summary"][:5]:
    key = tuple(ps["pair"])
    s_strict = s_tie = obs = 0
    for blk in doc["blocks"]:
        for pr in blk["pairs"]:
            if tuple(pr["pair"]) == key:
                obs += 1
                s_strict += pr["strict_inversion_count"]
                s_tie += pr["tie_change_count"]
    check(
        f"aggregate {key} consistent",
        ps["strict_inversion_events"] == s_strict
        and ps["tie_change_events"] == s_tie
        and ps["exclusions_observed"] == obs
        and ps["scenario_comparisons"] == obs * 1000,
    )
check("most influential is one of the excluded codes",
      agg["most_influential_excluded_code"] in [s["code"] for s in snapshot["samples"]])

# 6. Counterexample evidence is reproducible and correctly labeled.
for blk in doc["blocks"]:
    for pr in blk["pairs"]:
        for kind in ("strict_inversion", "tie_change"):
            for ex in pr["examples"][kind]:
                # Stored scenario weight must equal the original draw.
                draw = full["draws"][ex["scenario_index"]]
                assert np.allclose(ex["weights"], draw["weights"])
                # Label agrees with the actual relation transition.
                got = _transition(
                    {"greater": 1, "less": -1, "tie": 0}[ex["relation_before"]],
                    {"greater": 1, "less": -1, "tie": 0}[ex["relation_after"]],
                )
                assert got == kind, (kind, got, ex)
                assert len(pr["examples"][kind]) <= 3
check("all counterexamples reproduce stored draws and correct labels", True)

# 7. Resumability: feeding finished blocks yields the identical document.
seen = {}
progress_log = []
def partial_progress(done, total, code, resumed):
    progress_log.append((done, code, resumed))
    if done == 4:
        raise RuntimeError("simulated interruption")


half = doc["blocks"][:4]
for blk in half:
    seen[blk["excluded_code"]] = blk
try:
    build_diagnosis(run, progress=partial_progress, finished_blocks=seen)
except RuntimeError:
    pass
resumed = build_diagnosis(
    run,
    progress=lambda done, total, code, resumed=False: progress_log.append(
        (done, code, resumed)
    ),
    finished_blocks=seen,
)
check("resumed doc identical to full doc (fingerprint)",
      resumed["diagnosis_fingerprint"] == doc["diagnosis_fingerprint"])
check("resumed doc blocks identical",
      all(a == b for a, b in zip(resumed["blocks"], doc["blocks"])))
resumed_events = [x for x in progress_log if x[0] > 4]
check("resumed run reports resumed=True for cached blocks",
      all(x[2] for x in resumed_events if x[1] in seen))

# 8. Determinism: a fresh build has the same fingerprint.
again = build_diagnosis(run)
check("deterministic fingerprint across builds",
      again["diagnosis_fingerprint"] == doc["diagnosis_fingerprint"])

# 9. Hand-built deterministic evidence: one strict inversion and one tie change.
inv_matrix = np.array([
    [9.02887575, 8.83810285, 1.43333366],
    [6.28620061, 8.93626806, 2.08272626],
    [4.20593805, 7.16023636, 7.4072157],
    [4.56666048, 6.42232391, 6.72229701],
])
inv_w = np.array([0.43089747, 0.33078418, 0.23831835])
inv_dirs = ["benefit"] * 3
fn, _ = normalize_matrix(inv_matrix)
fs, _, _ = score_batch(fn, inv_dirs, inv_w[None, :])
rn, _ = normalize_matrix(inv_matrix[[0, 1, 2]])  # exclude row 3
rs, _, _ = score_batch(rn, inv_dirs, inv_w[None, :])
inv_got = _transition(
    int(np.sign(fs[0, 0] - fs[0, 2])),
    int(np.sign(rs[0, 0] - rs[0, 2])),
)
check("hand-built strict inversion (rows 0 vs 2, excluding row 3)",
      inv_got == "strict_inversion", inv_got)

tie_matrix = np.array([[8., 2.], [2., 8.], [9., 5.]])
tie_dirs = ["benefit", "benefit"]
tie_w = np.array([0.5, 0.5])
tn, _ = normalize_matrix(tie_matrix)
ts, _, _ = score_batch(tn, tie_dirs, tie_w[None, :])
trn, _ = normalize_matrix(tie_matrix[[0, 1]])  # exclude row 2
trs, _, _ = score_batch(trn, tie_dirs, tie_w[None, :])
tie_got = _transition(
    int(np.sign(ts[0, 0] - ts[0, 1])),
    int(np.sign(trs[0, 0] - trs[0, 1])),
)
check("hand-built strict->tie change (rows 0 vs 1, excluding row 2)",
      tie_got == "tie_change", tie_got)

# 10. End-to-end pipeline actually records the hand-built tie change when the
# tie matrix lives inside a frozen run with stored scenarios.
tie_snap = {
    "samples": [
        {"id": 1, "code": "T1", "name": "T1", "x": 8.0, "y": 2.0},
        {"id": 2, "code": "T2", "name": "T2", "x": 2.0, "y": 8.0},
        {"id": 3, "code": "T3", "name": "T3", "x": 9.0, "y": 5.0},
        {"id": 4, "code": "T4", "name": "T4", "x": 7.0, "y": 7.0},
    ],
    "criteria": [
        {"id": 1, "code": "x", "name": "X", "unit": "u",
         "direction": "benefit", "center_weight": 0.5},
        {"id": 2, "code": "y", "name": "Y", "unit": "u",
         "direction": "benefit", "center_weight": 0.5},
    ],
}
tie_run_result = analyze_decision(
    tie_snap, {"x": 0.5, "y": 0.5}, concentration=40,
    repeats=1000, seed=42,
)
tie_doc = build_diagnosis({
    "id": 7, "snapshot": tie_snap,
    "center_weights": {"x": 0.5, "y": 0.5},
    "result": tie_run_result,
})
t3_block = next(b for b in tie_doc["blocks"] if b["excluded_code"] == "T3")
t_pair = next(p for p in t3_block["pairs"] if p["pair"] == ["T1", "T2"])
check("pipeline records center-weight tie_change for T1/T2 when T3 removed",
      t_pair["center_change"] == "tie_change", t_pair["center_change"])
check(
    "tie block keeps norm + ideal-point before/after evidence",
    len(t3_block["norms_before"]) == 2
    and len(t3_block["ideal_best_delta"]) == 2
    and any(abs(d) > 0 for d in t3_block["norm_delta"]),
)

# 11. Manifest keeps constant-size headers; block files stay independently
# readable so the evidence store never needs one giant in-memory document.
from src.services.diagnosis import prepare_context, build_block  # noqa: E402
tctx = prepare_context({
    "id": 7, "snapshot": tie_snap,
    "center_weights": {"x": 0.5, "y": 0.5},
    "result": tie_run_result,
})
t_blocks = [build_block(tctx, i) for i in range(tctx["n"])]
manifest = build_manifest(tctx, t_blocks)
check("manifest blocks are headers without pairs",
      all("pairs" not in h and "changed_pair_count" in h
          for h in manifest["blocks"]))
check("manifest lists one block file per exclusion",
      manifest["block_files"] == [
          f"blocks/{s['code']}.json" for s in tctx["samples"]
      ])
check("header counts match its full block",
      all(
          block_header(b)["strict_inversion_events"]
          == b["strict_inversion_events"]
          and block_header(b)["tie_change_events"]
          == b["tie_change_events"]
          for b in t_blocks
      ))
check("manifest aggregate matches full document aggregate",
      manifest["aggregate"]["total_strict_inversion_events"]
      == tie_doc["aggregate"]["total_strict_inversion_events"]
      and manifest["diagnosis_fingerprint"]
      == tie_doc["diagnosis_fingerprint"])

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("all diagnosis oracle tests passed")
