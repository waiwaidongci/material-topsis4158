"""Queue, checkpoint and resume leave-one-out diagnoses.

A diagnosis is derived from one completed, frozen DecisionRun. The run row is
read-only here; only the new loo_diagnoses rows, their checkpoint blocks and
the artifact are written.

Two complementary stores make interruption cheap and safe:

* ``loo_diagnosis_blocks`` holds one slim projection per excluded candidate,
  inserted exactly once, so a restart skips finished exclusions with O(n)
  total database writes (no growing JSON is rewritten on every checkpoint);
* a per-exclusion sidecar file under the shared artifact directory holds the
  full counterexample evidence needed to seal the artifact after a restart.

Once the artifact is sealed the sidecar directory is removed; the database
keeps only slim projections.
"""
import json
from pathlib import Path

from fastapi import HTTPException
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import defer

from src.config.settings import REDIS_URL
from src.models import DecisionRun, LooDiagnosis, LooDiagnosisBlock
from src.services.diagnosis import (
    build_block,
    build_manifest,
    slim_block,
    prepare_context,
)

redis = Redis.from_url(REDIS_URL)
ARTIFACT_DIR = Path("/artifacts")


def list_diagnoses(db, run_id=None):
    stmt = (
        select(LooDiagnosis)
        .options(defer(LooDiagnosis.result))
        .order_by(LooDiagnosis.id.desc())
    )
    if run_id is not None:
        stmt = stmt.where(LooDiagnosis.run_id == run_id)
    return db.scalars(stmt).all()


def get_diagnosis(db, diagnosis_id):
    item = db.get(LooDiagnosis, diagnosis_id)
    if not item:
        raise HTTPException(404, "候选诊断不存在")
    return item


def get_block(db, diagnosis_id, excluded_code):
    """Return one slim block (pair counts, no counterexamples) from the
    checkpoint child table — the page reads changed-pair counts here while full
    counterexamples come from :func:`get_evidence`.
    """
    get_diagnosis(db, diagnosis_id)
    row = db.scalar(
        select(LooDiagnosisBlock).where(
            LooDiagnosisBlock.diagnosis_id == diagnosis_id,
            LooDiagnosisBlock.excluded_code == excluded_code,
        )
    )
    if not row:
        raise HTTPException(404, "该被剔除候选的检查点不存在")
    return row.block


def get_evidence(db, diagnosis_id, excluded_code=None):
    """Return full counterexample blocks from the per-exclusion artifact files.

    The database row only keeps slim projections and block headers; the
    complete reproducible evidence (scenario index, weights, before/after
    scores and ranks) is read on demand from the block files referenced by the
    sealed manifest on the shared /artifacts volume.
    """
    item = get_diagnosis(db, diagnosis_id)
    if item.status != "completed":
        raise HTTPException(409, "诊断尚未完成，暂无反例证据")
    manifest_path = Path(item.artifact_path)
    if not manifest_path.exists():
        raise HTTPException(409, "证据清单不可读，请检查共享 /artifacts 卷")
    manifest = json.loads(manifest_path.read_text())
    block_dir = manifest_path.parent / "blocks"

    wanted = [excluded_code] if excluded_code is not None else [
        Path(p).stem for p in manifest.get("block_files", [])
    ]
    if excluded_code is not None and not (block_dir / f"{excluded_code}.json").exists():
        raise HTTPException(404, "该被剔除候选没有对应的诊断块")

    focused = []
    for code in wanted:
        path = block_dir / f"{code}.json"
        if not path.exists():
            raise HTTPException(409, f"反例文件缺失：{code}")
        block = json.loads(path.read_text())
        # Only changed pairs carry counterexample digests worth returning.
        changed = [
            pr for pr in block["pairs"]
            if pr["strict_inversion_count"]
            or pr["tie_change_count"]
            or pr["center_change"] != "unchanged"
        ]
        focused.append({**block, "pairs": changed})
    return {
        "diagnosis_id": diagnosis_id,
        "diagnosis_fingerprint": manifest.get("diagnosis_fingerprint"),
        "artifact_path": str(manifest_path),
        "rank_rule": manifest.get("rank_rule"),
        "change_rule": manifest.get("change_rule"),
        "blocks": focused,
    }


def _checkpoint_rows(db, diagnosis_id):
    """Map excluded code -> slim block for already committed exclusions."""
    rows = db.scalars(
        select(LooDiagnosisBlock)
        .where(LooDiagnosisBlock.diagnosis_id == diagnosis_id)
    ).all()
    return {row.excluded_code: row.block for row in rows}


def create_diagnosis(db, data):
    run = db.get(DecisionRun, data.run_id)
    if not run:
        raise HTTPException(404, "原权衡任务不存在")
    if run.status != "completed":
        raise HTTPException(422, "只能对已完成并冻结的权衡任务发起候选诊断")
    if not run.result or not run.result.get("draws"):
        raise HTTPException(422, "原权衡未保留权重情景，无法沿用同一批情景")
    if db.scalar(
        select(LooDiagnosis).where(LooDiagnosis.name == data.name)
    ):
        raise HTTPException(409, "诊断名称已存在")
    active = db.scalar(
        select(func.count())
        .select_from(LooDiagnosis)
        .where(LooDiagnosis.status.in_(["queued", "running"]))
    )
    if active >= 3:
        raise HTTPException(429, "已有3项活动诊断，请等待完成")

    total = len(run.snapshot["samples"])
    item = LooDiagnosis(
        run_id=run.id,
        name=data.name,
        status="queued",
        # Constant-size progress; per-block rows live in the child table.
        progress={"total": total, "done": 0, "last_excluded": ""},
        summary={"run_id": run.id, "excluded_candidates": total},
        result={},
        error="",
        artifact_path="",
    )
    db.add(item)
    db.commit()
    try:
        redis.lpush("material:diagnoses", str(item.id))
    except Exception:
        pass
    return item


def execute_diagnosis(db, diagnosis_id):
    item = get_diagnosis(db, diagnosis_id)
    if item.status not in ("queued", "running"):
        return
    run = db.get(DecisionRun, item.run_id)
    if not run or run.status != "completed":
        item.status = "failed"
        item.error = "原权衡任务不可用或未完成，诊断中止"
        db.commit()
        return

    item.status = "running"
    db.commit()

    try:
        ctx = prepare_context(run)
        total = ctx["n"]
        ordered_codes = [s["code"] for s in ctx["samples"]]

        # Full evidence is kept one file per exclusion under a persistent
        # directory on the shared volume. Files survive a worker restart and
        # back the read-on-demand evidence endpoint; they are never merged into
        # one giant JSON, which would exceed the MySQL packet limit at 120
        # candidates. A small manifest seals the run beside the block files.
        diag_dir = ARTIFACT_DIR / f"diagnosis-{diagnosis_id}"
        block_dir = diag_dir / "blocks"
        block_dir.mkdir(parents=True, exist_ok=True)

        def block_path(code):
            return block_dir / f"{code}.json"

        slim_committed = _checkpoint_rows(db, diagnosis_id)
        full_blocks = {}
        for code in ordered_codes:
            path = block_path(code)
            if path.exists():
                full_blocks[code] = json.loads(path.read_text())

        done = len(slim_committed)
        for excluded_idx, excluded in enumerate(ctx["samples"]):
            code = excluded["code"]
            has_slim = code in slim_committed
            has_full = code in full_blocks
            if has_slim and has_full:
                continue
            # Rebuild when either store is missing (e.g. crash between the
            # block-file write and the checkpoint row commit).
            block = build_block(ctx, excluded_idx)
            full_blocks[code] = block
            projection = slim_block(block)

            # Seal the full block file atomically.
            btmp = block_path(code).with_suffix(".json.tmp")
            btmp.write_text(
                json.dumps(block, ensure_ascii=False, allow_nan=False)
            )
            btmp.replace(block_path(code))

            # Insert the slim projection only if an earlier process did not.
            if not has_slim:
                db.add(LooDiagnosisBlock(
                    diagnosis_id=diagnosis_id,
                    excluded_code=code,
                    block=projection,
                ))
                slim_committed[code] = projection
            done = len(slim_committed)
            item.progress = {
                "total": total,
                "done": done,
                "last_excluded": code,
            }
            Path("/tmp/heartbeat").touch()
            # Commit per block: an interruption loses at most this one block.
            db.commit()

        ordered_full = [full_blocks[c] for c in ordered_codes]
        # Manifest holds metadata, aggregate and constant-size block headers.
        manifest = build_manifest(ctx, ordered_full)

        ARTIFACT_DIR.mkdir(exist_ok=True)
        artifact = diag_dir / "manifest.json"
        tmp = artifact.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False)
        )
        tmp.replace(artifact)

        item.artifact_path = str(artifact)
        # The database row mirrors the small manifest only; pair detail stays
        # in the child table, full evidence in the per-exclusion block files.
        item.result = manifest
        item.summary = _summary(manifest, run)
        item.error = ""
        item.status = "completed"
        item.progress = {
            "total": total,
            "done": total,
            "last_excluded": ordered_full[-1]["excluded_code"],
        }
        db.commit()
    except Exception as exc:  # noqa: BLE001 - surface reason in the row
        db.rollback()
        item = get_diagnosis(db, diagnosis_id)
        item.status = "failed"
        item.error = str(exc)[:600]
        db.commit()


def _summary(document, run):
    agg = document["aggregate"]
    return {
        "run_id": run.id,
        "run_name": run.name,
        "excluded_candidates": document["excluded_candidates"],
        "remaining_per_diagnosis": document["remaining_per_diagnosis"],
        "scenarios_reused": document["scenarios_reused"],
        "total_strict_inversion_events": agg[
            "total_strict_inversion_events"
        ],
        "total_tie_change_events": agg["total_tie_change_events"],
        "center_weight_strict_inversion_pairs": agg[
            "center_weight_strict_inversion_pairs"
        ],
        "center_weight_tie_change_pairs": agg[
            "center_weight_tie_change_pairs"
        ],
        "most_influential_excluded_code": agg[
            "most_influential_excluded_code"
        ],
        "baseline_recheck": document["baseline_recheck"],
        "diagnosis_fingerprint": document["diagnosis_fingerprint"],
    }
