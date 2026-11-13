from fastapi import APIRouter, Depends

from src.config.database import get_db
from src.utils.auth import current_user, editor
from src.schemas.diagnosis import (
    DiagnosisInput,
    DiagnosisOut,
    DiagnosisSummary,
)
from src.controllers import diagnosis_controller as controller

router = APIRouter(prefix="/diagnoses", tags=["diagnoses"])


@router.get("", response_model=list[DiagnosisSummary])
def index(run_id: int | None = None, db=Depends(get_db),
          user=Depends(current_user)):
    return controller.index(db, run_id)


@router.post("", response_model=DiagnosisOut, status_code=201)
def create(data: DiagnosisInput, db=Depends(get_db),
           user=Depends(editor)):
    return controller.create(db, data)


@router.get("/{diagnosis_id}", response_model=DiagnosisOut)
def detail(diagnosis_id: int, db=Depends(get_db),
           user=Depends(current_user)):
    return controller.detail(db, diagnosis_id)


@router.get("/{diagnosis_id}/blocks/{excluded_code}")
def block(diagnosis_id: int, excluded_code: str,
          db=Depends(get_db), user=Depends(current_user)):
    return controller.block(db, diagnosis_id, excluded_code)


@router.get("/{diagnosis_id}/evidence")
def evidence(diagnosis_id: int, excluded_code: str | None = None,
             db=Depends(get_db), user=Depends(current_user)):
    return controller.evidence(db, diagnosis_id, excluded_code)
