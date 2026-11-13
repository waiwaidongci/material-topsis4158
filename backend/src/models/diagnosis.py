from datetime import datetime
from sqlalchemy import String, ForeignKey, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class LooDiagnosis(Base):
    """Leave-one-out candidate diagnosis derived from a completed run.

    The referenced DecisionRun is never modified. Each excluded candidate is
    checkpointed as one slim :class:`LooDiagnosisBlock` row so an interrupted
    diagnosis resumes without redoing finished exclusions. Full counterexample
    evidence is assembled into the artifact, not the database.
    """

    __tablename__ = "loo_diagnoses"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("decision_runs.id")
    )
    name: Mapped[str] = mapped_column(String(140), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # Constant-size checkpoint state: {"total": n, "done": k, "last_excluded"}.
    # Per-block data lives in loo_diagnosis_blocks to avoid rewriting a growing
    # JSON document on every checkpoint.
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    # Final slim document with the aggregate and all block projections.
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(600), default="")
    artifact_path: Mapped[str] = mapped_column(String(260), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class LooDiagnosisBlock(Base):
    """One committed leave-one-out block (slim projection, no examples).

    A row is inserted once per excluded candidate, giving O(n) total checkpoint
    writes instead of rewriting the whole block map on every step.
    """

    __tablename__ = "loo_diagnosis_blocks"
    __table_args__ = (
        UniqueConstraint("diagnosis_id", "excluded_code",
                         name="uq_loo_block_diagnosis_excluded"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("loo_diagnoses.id")
    )
    excluded_code: Mapped[str] = mapped_column(String(40))
    block: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
