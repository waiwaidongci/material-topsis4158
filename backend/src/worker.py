import time
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import DecisionRun, LooDiagnosis
from src.services.run_service import execute_run
from src.services.diagnosis_service import execute_diagnosis

redis = Redis.from_url(REDIS_URL)


def next_item(db):
    """Oldest active run or diagnosis; the database row is authoritative.

    Runs keep priority so a queued trade-off is not starved by diagnoses, but
    either kind is picked up even if the Redis wake-up is lost.
    """
    run = db.scalar(
        select(DecisionRun)
        .where(DecisionRun.status.in_(["queued", "running"]))
        .order_by(DecisionRun.id)
    )
    if run:
        return ("run", run.id)
    diagnosis = db.scalar(
        select(LooDiagnosis)
        .where(LooDiagnosis.status.in_(["queued", "running"]))
        .order_by(LooDiagnosis.id)
    )
    if diagnosis:
        return ("diagnosis", diagnosis.id)
    return None


while True:
    Path("/tmp/heartbeat").touch()
    with Session() as db:
        item = next_item(db)
        if item:
            kind, item_id = item
            if kind == "run":
                execute_run(db, item_id)
            else:
                execute_diagnosis(db, item_id)
        else:
            # Block on either wake-up queue; the DB scan remains the fallback.
            redis.brpop(["material:runs", "material:diagnoses"], timeout=2)
    time.sleep(0.1)
