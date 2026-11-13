"""End-to-end API test through the FastPI app, auth and middleware on SQLite.

Run from repo root:
  PYTHONPATH=backend /tmp/venv/bin/python test-artifacts/gb-0024/test_api.py
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="loo-api-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/api.db"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
os.environ["SEED_DEMO"] = "false"
os.environ.setdefault("JWT_SECRET", "test-secret")

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# No Redis in the test environment: neutralize the login rate limiter.
import src.middlewares.rate_limit as rl  # noqa: E402


class _FakeRedis:
    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()

    def incr(self, key):
        return 1

    def expire(self, key, seconds):
        return True

    def ping(self):
        return True

    def lpush(self, *args, **kwargs):
        return 1

    def brpop(self, *args, **kwargs):
        return None


rl.Redis = _FakeRedis

import src.main as main_mod  # noqa: E402
main_mod.Redis = _FakeRedis

from src.main import app  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.models import User, DecisionRun  # noqa: E402
from src.services.weight_sensitivity import analyze_decision  # noqa: E402
from src.services import diagnosis_service  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from pathlib import Path  # noqa: E402

diagnosis_service.ARTIFACT_DIR = Path(_tmp) / "artifacts"
fails = []


def check(name, cond, detail=""):
    print(f"[{'ok' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


# Seed users and a completed run directly (no SEED_DEMO).
rng = np.random.default_rng(20261112)
n = 8
samples = []
for i in range(n):
    row = {"id": i + 1, "code": f"LY{i+1:02}", "name": f"s{i}"}
    for j, k in enumerate(["absorption", "impact", "carbon", "price", "thickness"]):
        row[k] = float(rng.uniform(0.2, 180))
    samples.append(row)
criteria = [
    {"id": j + 1, "code": k, "name": k, "unit": "u", "direction": d, "center_weight": w}
    for j, (k, d, w) in enumerate([
        ("absorption", "benefit", 0.3), ("impact", "benefit", 0.2),
        ("carbon", "cost", 0.2), ("price", "cost", 0.2), ("thickness", "cost", 0.1)])
]
snapshot = {"samples": samples, "criteria": criteria}
center = {c["code"]: c["center_weight"] for c in criteria}
full = analyze_decision(snapshot, center, 40, 1000, 20261112)

with TestClient(app) as client:
    with Session() as db:
        for role, email in [("analyst", "analyst@luyan.local"),
                            ("viewer", "viewer@luyan.local")]:
            db.add(User(email=email, name=role, role=role,
                        password_hash=hash_password("Luyan2026!")))
        run = DecisionRun(name="r1", study_id=1, center_weights=center,
                          concentration=40.0, repeats=1000, seed=20261112,
                          status="completed", snapshot=snapshot, result=full,
                          summary={"samples": n}, error="")
        db.add(run)
        db.commit()
        run_id = run.id

    def login(email):
        resp = client.post("/auth/login",
                           json={"email": email, "password": "Luyan2026!"})
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    analyst = login("analyst@luyan.local")
    viewer = login("viewer@luyan.local")
    ah = {"Authorization": f"Bearer {analyst}"}
    vh = {"Authorization": f"Bearer {viewer}"}

    # Auth required.
    check("unauthenticated list rejected",
          client.get("/diagnoses").status_code == 401)

    # Viewer cannot create (editor dependency).
    r = client.post("/diagnoses", headers=vh,
                    json={"name": "viewer-diag", "run_id": run_id})
    check("viewer create -> 403", r.status_code == 403, str(r.status_code))

    # Validation: missing run.
    r = client.post("/diagnoses", headers=ah,
                    json={"name": "ghost", "run_id": 9999})
    check("missing run -> 404", r.status_code == 404)

    # Name length validation.
    r = client.post("/diagnoses", headers=ah, json={"name": "x", "run_id": run_id})
    check("short name -> 422", r.status_code == 422)

    # Analyst creates a diagnosis.
    r = client.post("/diagnoses", headers=ah,
                    json={"name": "loo-api-1", "run_id": run_id})
    check("create -> 201", r.status_code == 201, r.text)
    diag_id = r.json()["id"]
    check("created queued", r.json()["status"] == "queued")

    # Duplicate name -> 409.
    r = client.post("/diagnoses", headers=ah,
                    json={"name": "loo-api-1", "run_id": run_id})
    check("duplicate name -> 409", r.status_code == 409)

    # List (summary only, no result/progress heavy columns deferred).
    r = client.get("/diagnoses", headers=ah)
    check("list 200", r.status_code == 200 and len(r.json()) == 1)
    check("list filtered by run",
          client.get(f"/diagnoses?run_id={run_id}", headers=ah).json()[0]["id"] == diag_id)
    check("unknown run filter empty",
          client.get("/diagnoses?run_id=9999", headers=ah).json() == [])

    # Run the worker logic to completion.
    with Session() as db:
        diagnosis_service.execute_diagnosis(db, diag_id)

    # Detail shows completed result with separated counts and evidence refs.
    r = client.get(f"/diagnoses/{diag_id}", headers=vh)  # viewer can read
    body = r.json()
    check("viewer can read detail 200", r.status_code == 200)
    check("status completed", body["status"] == "completed", body.get("error"))
    res = body["result"]
    check("n block headers", len(res["blocks"]) == n)
    check("strict/tie separated",
          "total_strict_inversion_events" in res["aggregate"]
          and "total_tie_change_events" in res["aggregate"])
    check("baseline exact",
          res["baseline_recheck"]["stored_scenarios_match_recompute"] is True)
    check("result blocks are headers (no pairs/examples)",
          all("pairs" not in b and "examples" not in b
              for b in res["blocks"]))
    check("norm/ideal deltas present per header",
          all(len(b["norm_delta"]) == 5 and len(b["ideal_best_delta"]) == 5
              for b in res["blocks"]))
    check("manifest references n block files",
          len(res["block_files"]) == n)
    check("artifact path is manifest.json",
          body["artifact_path"].endswith(
              f"diagnosis-{diag_id}/manifest.json"))

    # Block endpoint returns the slim checkpoint (pair counts, no examples).
    code0 = res["blocks"][0]["excluded_code"]
    r = client.get(f"/diagnoses/{diag_id}/blocks/{code0}", headers=vh)
    check("block endpoint 200", r.status_code == 200)
    blk = r.json()
    check("block has pairs without example digests",
          "pairs" in blk and all("examples" not in p for p in blk["pairs"]))
    check("unknown block -> 404",
          client.get(f"/diagnoses/{diag_id}/blocks/NOPE",
                     headers=vh).status_code == 404)

    # Evidence endpoint reads the sealed artifact with full counterexamples.
    r = client.get(f"/diagnoses/{diag_id}/evidence", headers=vh)
    ev = r.json()
    check("evidence 200", r.status_code == 200)
    check("evidence fingerprint matches",
          ev["diagnosis_fingerprint"] == res["diagnosis_fingerprint"])
    check("evidence blocks carry full examples",
          all("examples" in pr for b in ev["blocks"] for pr in b["pairs"]))
    check("evidence only includes changed pairs",
          all(pr["strict_inversion_count"] or pr["tie_change_count"]
              or pr["center_change"] != "unchanged"
              for b in ev["blocks"] for pr in b["pairs"]))
    first_code = res["blocks"][0]["excluded_code"]
    r = client.get(
        f"/diagnoses/{diag_id}/evidence?excluded_code={first_code}", headers=vh)
    check("evidence filtered to one exclusion",
          r.status_code == 200 and len(r.json()["blocks"]) == 1
          and r.json()["blocks"][0]["excluded_code"] == first_code)
    check("unknown exclusion -> 404",
          client.get(f"/diagnoses/{diag_id}/evidence?excluded_code=NOPE",
                     headers=vh).status_code == 404)

    # 404 detail.
    check("detail 404", client.get("/diagnoses/9999", headers=ah).status_code == 404)

    # Health still works.
    check("health ok", client.get("/health").status_code == 200)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    raise SystemExit(1)
print("api tests passed")
