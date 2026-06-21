# app/routers/hospitals.py

import uuid
from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Optional
from app.batch_processor import (
    parse_csv,
    process_bulk_background,
    resume_bulk_background,
    job_store,
    JobStatus,
)
from app.models import BulkCreateResponse

router = APIRouter(prefix="/hospitals", tags=["hospitals"])


# --- Validation models ---

class ValidationError(BaseModel):
    row: int
    error: str


class CSVValidationResponse(BaseModel):
    valid: bool
    total_rows: int
    errors: list[ValidationError]


class BulkAcceptedResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


class JobProgressResponse(BaseModel):
    job_id: str
    status: JobStatus
    total: int
    completed: int
    failed_count: int
    result: Optional[BulkCreateResponse] = None
    error: Optional[str] = None


# --- Endpoints ---

@router.post(
    "/validate-csv",
    response_model=CSVValidationResponse,
)
async def validate_csv(file: UploadFile = File(...)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        rows = parse_csv(contents)
        return CSVValidationResponse(valid=True, total_rows=len(rows), errors=[])
    except ValueError as e:
        raw = str(e)
        parts = raw.split(" | ")
        errors = []
        for part in parts:
            if part.startswith("Row "):
                try:
                    row_num = int(part.split(":")[0].replace("Row ", ""))
                    errors.append(ValidationError(row=row_num, error=part))
                except ValueError:
                    errors.append(ValidationError(row=0, error=part))
            else:
                errors.append(ValidationError(row=0, error=part))
        return CSVValidationResponse(valid=False, total_rows=0, errors=errors)


# app/routers/hospitals.py - replace bulk_create_hospitals endpoint

@router.post(
    "/bulk",
    response_model=BulkAcceptedResponse,
    status_code=202,
)
async def bulk_create_hospitals(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    contents = await file.read()

    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Validate CSV eagerly BEFORE accepting the job
    # If CSV is invalid, fail fast with 422 — don't waste a job_id
    try:
        parse_csv(contents)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    job_id = str(uuid.uuid4())
    job_store.create(job_id)

    background_tasks.add_task(process_bulk_background, job_id, contents)

    return BulkAcceptedResponse(
        job_id=job_id,
        status="accepted",
        poll_url=f"/hospitals/bulk/{job_id}",
    )


@router.get(
    "/bulk/{job_id}",
    response_model=JobProgressResponse,
)
async def get_bulk_status(job_id: str):
    state = job_store.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return JobProgressResponse(
        job_id=job_id,
        status=state["status"],
        total=state["total"],
        completed=state["completed"],
        failed_count=state["failed_count"],
        result=state["result"],
        error=state["error"],
    )


@router.post(
    "/bulk/{job_id}/resume",
    response_model=BulkAcceptedResponse,
    status_code=202,
)
async def resume_bulk(job_id: str, background_tasks: BackgroundTasks):
    """
    Resume a failed or partially completed bulk job.
    Retries only the failed rows using the same batch_id.
    """
    state = job_store.get(job_id)

    if state is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")

    if state["status"] == JobStatus.DONE and state["result"].batch_activated:
        raise HTTPException(
            status_code=400,
            detail="Job already completed successfully. Nothing to resume.",
        )

    if state["status"] == JobStatus.PROCESSING:
        raise HTTPException(
            status_code=400,
            detail="Job is still processing. Wait for it to finish before resuming.",
        )

    background_tasks.add_task(resume_bulk_background, job_id)

    return BulkAcceptedResponse(
        job_id=job_id,
        status="resuming",
        poll_url=f"/hospitals/bulk/{job_id}",
    )