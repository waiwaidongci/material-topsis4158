"""Service-level integration test: queue, checkpoint, interrupt, resume.

Uses an isolated SQLite database and a temp artifact dir so nothing in the
real MySQL/Redis/artifact state is touched. Run from repo root:

  DATABASE_URL='sqlite:////tmp/loo_integ.db' \
  PYTHONPATH=backend /tmp/venv/bin/python test-artifacts/gb-0024/test_integration.py
"""
import json
import os
import tempfile
from pathlib import Path

import numpy as np

# Redirect storage before importing app modules.
_tmp = tempfile.mkdtemp(prefix="loo-integ-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/integ.db"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"  # nothing listening
os.environ.setdefault("JWT_SECRET", "test-secret")

import src.services.diagnosis_service as ds  # noqa: E402
from src.config.database import Base, Session, engine  # noqa: E402
from src.models import DecisionRun, LooDiagnosis, LooDiagnosisBlock  # noqa: E402
from src.schemas.diagnosis import DiagnosisInput  # noqa: E402
from src.services.weight_sensitivity import analyze_decision  # noqa: E402
from src.services import diagnosis_service as service  # noqa: E402

ART = Path(_tmp) / "artifacts"
ds.ARTIFACT_DIR = ART

fails = []


def check(name, cond, detail=""):
    print(f"[{'ok' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def expect_404(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return False
    except Exception as exc:
        return getattr(exc, "status_code", None) == 404


Base.metadata.create_all(engine)

# Build a frozen, completed run the same way run_service.execute_run would.
rng = np.random.default_rng(20261112)
n, m = 12, 5
samples = []
for i in range(n):
    row = {"id": i + 1, "code": f"LY{i+1:02}", "name": f"s{i}"}
    for j, k in enumerate(
        ["absorption", "impact", "carbon", "price", "thickness"]
    ):
        row[k] = float(rng.uniform(0.2, 180))
    samples.append(row)
criteria = [
    {"id": j + 1, "code": k, "name": k, "unit": "u", "direction": d,
     "center_weight": w}
    for j, (k, d, w) in enumerate([
        ("absorption", "benefit", 0.3), ("impact", "benefit", 0.2),
        ("carbon", "cost", 0.2), ("price", "cost", 0.2),
        ("thickness", "cost", 0.1),
    ])
]
snapshot = {"samples": samples, "criteria": criteria}
center = {c["code"]: c["center_weight"] for c in criteria}
full = analyze_decision(snapshot, center, 40, 1000, 20261112)

with Session() as db:
    run = DecisionRun(
        name="frozen-run", study_id=1, center_weights=center,
        concentration=40.0, repeats=1000, seed=20261112,
        status="completed", snapshot=snapshot, result=full,
        summary={"samples": n}, error="",
    )
    db.add(run)
    db.commit()
    run_id = run.id
    run_fingerprint_before = full["fingerprint"]
    run_snapshot_before = json.dumps(snapshot, sort_keys=True)

# 1. Validation: cannot diagnose a non-completed run.
with Session() as db:
    bad = DecisionRun(
        name="queued-run", study_id=1, center_weights=center,
        concentration=40.0, repeats=1000, seed=1, status="queued",
        snapshot=snapshot, result={}, summary={}, error="",
    )
    db.add(bad)
    db.commit()
    try:
        service.create_diagnosis(
            db, DiagnosisInput(name="d-bad", run_id=bad.id)
        )
        check("reject non-completed run", False)
    except Exception as exc:
        check("reject non-completed run", "422" in str(exc) or "已完成" in str(exc))

# 2. Create a diagnosis (redis push fails silently with no server).
with Session() as db:
    item = service.create_diagnosis(
        db, DiagnosisInput(name="loo-1", run_id=run_id)
    )
    diagnosis_id = item.id
    check("diagnosis created queued", item.status == "queued")

# 3. Simulate a worker crash after 4 blocks are committed.
real_build_block = ds.build_block
calls = {"n": 0}


def crashing_build_block(ctx, excluded_idx):
    block = real_build_block(ctx, excluded_idx)
    calls["n"] += 1
    if calls["n"] == 4:
        raise RuntimeError("simulated worker interruption")
    return block


ds.build_block = crashing_build_block
with Session() as db:
    service.execute_diagnosis(db, diagnosis_id)
with Session() as db:
    item = db.get(LooDiagnosis, diagnosis_id)
    check("status failed after crash", item.status == "failed", item.status)
    check(
        "three blocks checkpointed before crash",
        item.progress["done"] == 3,
        f"done={item.progress['done']}",
    )
    block_rows = ds._checkpoint_rows(db, diagnosis_id)
    check("three child block rows committed", len(block_rows) == 3)
    check("progress stays constant-size (no embedded blocks map)",
          "blocks" not in item.progress)
    check("error recorded", "simulated worker interruption" in item.error)

# 4. Resume: reset status like a restarted worker would pick up 'running';
#    the service treats queued/running as workable, so flip failed->queued.
resume_calls = {"n": 0}


def counting_build_block(ctx, excluded_idx):
    resume_calls["n"] += 1
    return real_build_block(ctx, excluded_idx)


ds.build_block = counting_build_block
with Session() as db:
    item = db.get(LooDiagnosis, diagnosis_id)
    item.status = "queued"
    db.commit()
with Session() as db:
    service.execute_diagnosis(db, diagnosis_id)
check(
    "resume rebuilt only missing blocks (not cached ones)",
    resume_calls["n"] == n - 3,
    f"rebuilt {resume_calls['n']} of {n} (3 cached)",
)
with Session() as db:
    item = db.get(LooDiagnosis, diagnosis_id)
    check("resume reaches completed", item.status == "completed", item.error)
    check("all exclusions done", item.progress["done"] == n)
    final_rows = ds._checkpoint_rows(db, diagnosis_id)
    check("one checkpoint child row per exclusion", len(final_rows) == n)
    diag_dir = ART / f"diagnosis-{diagnosis_id}"
    check("evidence block directory retained after sealing",
          diag_dir.exists() and (diag_dir / "blocks").is_dir())
    check("n per-exclusion block files present",
          len(list((diag_dir / "blocks").glob("*.json"))) == n)
    check("result present", bool(item.result))
    check(
        "DB result blocks are constant-size headers (no pairs)",
        all("pairs" not in blk and "changed_pair_count" in blk
            for blk in item.result["blocks"]),
    )
    check("manifest references every block file",
          len(item.result["block_files"]) == n)
    check("artifact path is the manifest", item.artifact_path.endswith(
        f"diagnosis-{diagnosis_id}/manifest.json"))
    agg = item.result["aggregate"]
    check(
        "strict and tie counts separated",
        "total_strict_inversion_events" in agg
        and "total_tie_change_events" in agg,
    )
    check(
        "baseline recheck exact",
        item.summary["baseline_recheck"][
            "stored_scenarios_match_recompute"] is True,
    )

# 5. Block files hold full counterexample evidence; the manifest is small.
manifest = Path(item.artifact_path)
doc = json.loads(manifest.read_text())
check("manifest exists", manifest.exists())
block_files = [diag_dir / p for p in doc["block_files"]]
check("every referenced block file exists", all(p.exists() for p in block_files))
first_block = json.loads(block_files[0].read_text())
check(
    "block files carry per-pair examples",
    all("examples" in pr for pr in first_block["pairs"]),
)
check(
    "manifest and DB share fingerprint",
    doc["diagnosis_fingerprint"] == item.summary["diagnosis_fingerprint"],
)
# Re-verify a strict-inversion counterexample against the stored draw.
example_found = False
for bp in block_files:
    blk = json.loads(bp.read_text())
    for pr in blk["pairs"]:
        for ex in pr["examples"]["strict_inversion"]:
            draw = full["draws"][ex["scenario_index"]]
            assert np.allclose(ex["weights"], draw["weights"])
            assert ex["relation_before"] != ex["relation_after"]
            example_found = True
check("block files contain reproducible strict-inversion counterexample",
      example_found)

# 5b. Block-detail endpoint helper returns the slim checkpoint block.
with Session() as db:
    slim = service.get_block(db, diagnosis_id, first_block["excluded_code"])
    check("get_block returns slim pairs without examples",
          "pairs" in slim and all("examples" not in pr for pr in slim["pairs"]))

# 5c. Evidence helper reads full blocks from the sealed directory.
with Session() as db:
    ev = service.get_evidence(db, diagnosis_id,
                              first_block["excluded_code"])
check("evidence returns one full block", len(ev["blocks"]) == 1
      and all("examples" in pr for pr in ev["blocks"][0]["pairs"]))
with Session() as db:
    check("evidence unknown exclusion -> 404",
          expect_404(service.get_evidence, db, diagnosis_id, "NOPE"))

# 6. Original run is never rewritten.
with Session() as db:
    run = db.get(DecisionRun, run_id)
    check("run still completed", run.status == "completed")
    check("run fingerprint unchanged",
          run.result["fingerprint"] == run_fingerprint_before)
    check("run snapshot unchanged",
          json.dumps(run.snapshot, sort_keys=True) == run_snapshot_before)

# 7. Listing endpoint helper returns summaries without heavy columns.
with Session() as db:
    listed = service.list_diagnoses(db)
    check("list returns the diagnosis", any(d.id == diagnosis_id for d in listed))
    by_run = service.list_diagnoses(db, run_id=run_id)
    check("filter by run works", all(d.run_id == run_id for d in by_run))

# 8. Idempotent re-execute of a completed diagnosis is a no-op.
with Session() as db:
    service.execute_diagnosis(db, diagnosis_id)
    item = db.get(LooDiagnosis, diagnosis_id)
    check("re-execute completed diagnosis is no-op",
          item.status == "completed")

# 9. Partial-store resume: a second diagnosis loses a block file while its
#    checkpoint row survives; the worker must rebuild just that block.
with Session() as db:
    second = service.create_diagnosis(
        db, DiagnosisInput(name="loo-2", run_id=run_id)
    )
    second_id = second.id

partial = {"n": 0}


def partial_build_block(ctx, excluded_idx):
    block = real_build_block(ctx, excluded_idx)
    partial["n"] += 1
    if partial["n"] == 5:
        raise RuntimeError("crash after block/row of earlier exclusions")
    return block


ds.build_block = partial_build_block
with Session() as db:
    service.execute_diagnosis(db, second_id)
# Simulate a lost block file for one already-checkpointed exclusion.
second_blocks = ART / f"diagnosis-{second_id}" / "blocks"
victim = sorted(second_blocks.glob("*.json"))[0]
victim.unlink()
rebuilt = {"n": 0}


def rebuild_counter(ctx, excluded_idx):
    rebuilt["n"] += 1
    return real_build_block(ctx, excluded_idx)


ds.build_block = rebuild_counter
with Session() as db:
    item = db.get(LooDiagnosis, second_id)
    item.status = "queued"
    db.commit()
with Session() as db:
    service.execute_diagnosis(db, second_id)
# Missing block file (1) plus never-built exclusions (n-4) are rebuilt; the
# other three fully-recovered exclusions are skipped.
with Session() as db:
    item = db.get(LooDiagnosis, second_id)
    check("partial-store resume completes",
          item.status == "completed", item.error)
check("partial resume rebuilt lost block + remaining exclusions",
      rebuilt["n"] == (n - 4) + 1, f"rebuilt={rebuilt['n']}")
with Session() as db:
    check("checkpoint rows exactly n (no duplicates)",
          len(ds._checkpoint_rows(db, second_id)) == n)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    raise SystemExit(1)
print("integration tests passed; artifacts in", _tmp)
