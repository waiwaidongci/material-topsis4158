from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class DiagnosisInput(BaseModel):
    name: str = Field(min_length=2, max_length=140)
    run_id: int


class DiagnosisSummary(BaseModel):
    id: int
    run_id: int
    name: str
    status: str
    progress: dict
    summary: dict
    error: str
    artifact_path: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DiagnosisOut(DiagnosisSummary):
    result: dict
