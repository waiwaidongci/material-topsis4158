from src.services import diagnosis_service as service


def index(db, run_id=None):
    return service.list_diagnoses(db, run_id)


def create(db, data):
    return service.create_diagnosis(db, data)


def detail(db, diagnosis_id):
    return service.get_diagnosis(db, diagnosis_id)


def evidence(db, diagnosis_id, excluded_code=None):
    return service.get_evidence(db, diagnosis_id, excluded_code)


def block(db, diagnosis_id, excluded_code):
    return service.get_block(db, diagnosis_id, excluded_code)
