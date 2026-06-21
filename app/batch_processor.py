# app/batch_processor.py

import asyncio
import csv
import io
import time
import uuid
from typing import Optional
import httpx

from app.hospital_client import create_hospital, activate_batch
from app.models import (
    BulkCreateResponse,
    HospitalResult,
    HospitalRow,
    HospitalStatus,
)
from app.config import CONCURRENCY_LIMIT, MAX_HOSPITALS
from enum import Enum as PyEnum

class JobStatus(str, PyEnum):
    PENDING     = "pending"
    PROCESSING  = "processing"
    DONE        = "done"
    FAILED      = "failed"


class JobState:
    def __init__(self):
        self._store: dict[str, dict] = {}

    def create(self, job_id: str):
        self._store[job_id] = {
            "status": JobStatus.PENDING,
            "total": 0,
            "completed": 0,
            "failed_count": 0,
            "result": None,
            "error": None,
            # Resume support: store original rows and batch_id
            "batch_id": None,
            "original_rows": [],       # all HospitalRow objects from CSV
            "succeeded_rows": set(),   # row numbers that succeeded
            "failed_results": [],      # HospitalResult objects that failed
            "succeeded_results": [],   # HospitalResult objects that succeeded
        }

    def update_progress(
        self,
        job_id: str,
        total: int,
        completed: int,
        failed_count: int,
    ):
        if job_id in self._store:
            self._store[job_id].update({
                "status": JobStatus.PROCESSING,
                "total": total,
                "completed": completed,
                "failed_count": failed_count,
            })

    def record_result(self, job_id: str, result: "HospitalResult"):
        """Track each row result for resume capability."""
        if job_id not in self._store:
            return
        if result.hospital_id is not None:
            self._store[job_id]["succeeded_rows"].add(result.row)
            self._store[job_id]["succeeded_results"].append(result)
        else:
            self._store[job_id]["failed_results"].append(result)

    def set_batch_id(self, job_id: str, batch_id: str):
        if job_id in self._store:
            self._store[job_id]["batch_id"] = batch_id

    def set_original_rows(self, job_id: str, rows: list):
        if job_id in self._store:
            self._store[job_id]["original_rows"] = rows

    def get_resumable_rows(self, job_id: str) -> list:
        """Return rows that failed and need to be retried."""
        state = self._store.get(job_id)
        if not state:
            return []
        succeeded = state["succeeded_rows"]
        return [r for r in state["original_rows"] if r.row not in succeeded]

    def mark_done(self, job_id: str, result: "BulkCreateResponse"):
        if job_id in self._store:
            self._store[job_id].update({
                "status": JobStatus.DONE,
                "result": result,
            })

    def mark_failed(self, job_id: str, error: str):
        if job_id in self._store:
            self._store[job_id].update({
                "status": JobStatus.FAILED,
                "error": error,
            })

    def mark_resumable(self, job_id: str):
        """Partial failure — job can be resumed."""
        if job_id in self._store:
            self._store[job_id]["status"] = JobStatus.FAILED

    def get(self, job_id: str) -> Optional[dict]:
        return self._store.get(job_id)


# Single global instance — shared across all requests
job_store = JobState()

def parse_csv(contents: bytes) -> list[HospitalRow]:
    """
    Parse and validate CSV bytes.
    Raises ValueError with all row errors combined.
    """
    text = contents.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        raise ValueError("CSV file is empty.")

    fieldnames = [f.strip().lower() for f in reader.fieldnames]
    if "name" not in fieldnames or "address" not in fieldnames:
        raise ValueError("CSV must have 'name' and 'address' columns.")

    rows = []
    row_errors = []

    for i, row in enumerate(reader, start=1):
        if i > MAX_HOSPITALS:
            row_errors.append(f"Row {i}: exceeds maximum of {MAX_HOSPITALS} hospitals.")
            continue

        name = row.get("name", "").strip()
        address = row.get("address", "").strip()
        phone = row.get("phone", "").strip() or None

        if not name and not address:
            row_errors.append(f"Row {i}: 'name' and 'address' are both empty.")
        elif not name:
            row_errors.append(f"Row {i}: 'name' is empty.")
        elif not address:
            row_errors.append(f"Row {i}: 'address' is empty.")
        else:
            rows.append(HospitalRow(row=i, name=name, address=address, phone=phone))

    if row_errors:
        raise ValueError(" | ".join(row_errors))

    if not rows:
        raise ValueError("CSV has no valid data rows.")

    return rows


async def _create_one(
    row: HospitalRow,
    batch_id: str,
    semaphore: asyncio.Semaphore,
    job_id: str,
    total: int,
    completed_counter: list,
) -> HospitalResult:
    async with semaphore:
        try:
            result = await create_hospital(
                name=row.name,
                address=row.address,
                phone=row.phone,
                batch_id=batch_id,
            )
            completed_counter[0] += 1
            job_store.update_progress(
                job_id=job_id,
                total=total,
                completed=completed_counter[0],
                failed_count=0,
            )
            hospital_result = HospitalResult(
                row=row.row,
                hospital_id=result.get("id"),
                name=row.name,
                status=HospitalStatus.FAILED,  # updated after activation
            )
            job_store.record_result(job_id, hospital_result)
            return hospital_result

        except httpx.TimeoutException as e:
            error_msg = f"Timeout: {type(e).__name__}"
        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"

        completed_counter[0] += 1
        failed_result = HospitalResult(
            row=row.row,
            hospital_id=None,
            name=row.name,
            status=HospitalStatus.FAILED,
            error=error_msg,
        )
        job_store.record_result(job_id, failed_result)
        return failed_result
    

async def process_bulk(contents: bytes) -> BulkCreateResponse:
    """
    Full pipeline:
      parse → generate batch_id → concurrent create → activate → respond
    """
    start_time = time.monotonic()

    # Step 1: Parse and validate CSV
    rows = parse_csv(contents)

    # Step 2: Generate unique batch ID for this upload
    batch_id = str(uuid.uuid4())

    # Step 3: Concurrent hospital creation — max 5 simultaneous calls
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks = [_create_one(row, batch_id, semaphore) for row in rows]
    results: list[HospitalResult] = await asyncio.gather(*tasks)

    # Step 4: Separate successes from failures
    succeeded = [r for r in results if r.hospital_id is not None]
    failed = [r for r in results if r.hospital_id is None]

    # Step 5: Activate batch only if ALL hospitals were created
    # Design decision: partial activation is messy — either full success or not.
    # If any failed, we skip activation and report honestly.
    batch_activated = False
    if len(succeeded) == len(rows):
        batch_activated = await activate_batch(batch_id)

    # Step 6: Update status on succeeded results post-activation
    if batch_activated:
        for r in succeeded:
            r.status = HospitalStatus.CREATED_AND_ACTIVATED

    elapsed = round(time.monotonic() - start_time, 3)

    return BulkCreateResponse(
        batch_id=batch_id,
        total_hospitals=len(rows),
        processed_hospitals=len(succeeded),
        failed_hospitals=len(failed),
        processing_time_seconds=elapsed,
        batch_activated=batch_activated,
        hospitals=sorted(results, key=lambda r: r.row),  # preserve CSV order
    )

async def process_bulk_background(job_id: str, contents: bytes):
    start_time = time.monotonic()

    try:
        rows = parse_csv(contents)
    except ValueError as e:
        job_store.mark_failed(job_id, str(e))
        return

    batch_id = str(uuid.uuid4())
    total = len(rows)
    completed_counter = [0]

    # Store for resume
    job_store.set_batch_id(job_id, batch_id)
    job_store.set_original_rows(job_id, rows)
    job_store.update_progress(job_id, total=total, completed=0, failed_count=0)

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks = [
        _create_one(row, batch_id, semaphore, job_id, total, completed_counter)
        for row in rows
    ]
    results: list[HospitalResult] = await asyncio.gather(*tasks)

    succeeded = [r for r in results if r.hospital_id is not None]
    failed = [r for r in results if r.hospital_id is None]

    # Only activate if ALL rows succeeded
    batch_activated = False
    if len(succeeded) == len(rows):
        batch_activated = await activate_batch(batch_id)

    if batch_activated:
        for r in succeeded:
            r.status = HospitalStatus.CREATED_AND_ACTIVATED

    elapsed = round(time.monotonic() - start_time, 3)

    final_result = BulkCreateResponse(
        batch_id=batch_id,
        total_hospitals=total,
        processed_hospitals=len(succeeded),
        failed_hospitals=len(failed),
        processing_time_seconds=elapsed,
        batch_activated=batch_activated,
        hospitals=sorted(results, key=lambda r: r.row),
    )

    if failed:
        # Partial failure — mark as failed but resumable
        job_store.mark_resumable(job_id)
        # Store partial result so polling shows what happened
        job_store.mark_done(job_id, final_result)
    else:
        job_store.mark_done(job_id, final_result)


async def resume_bulk_background(job_id: str):
    """
    Retry only the failed rows from a previous job.
    Uses the SAME batch_id so hospitals end up in the same batch.
    """
    state = job_store.get(job_id)
    if not state:
        return

    start_time = time.monotonic()
    batch_id = state["batch_id"]

    # Get only the rows that haven't succeeded yet
    rows_to_retry = job_store.get_resumable_rows(job_id)

    if not rows_to_retry:
        # Nothing to retry — just try activation again
        batch_activated = await activate_batch(batch_id)
        if batch_activated:
            state = job_store.get(job_id)
            existing_result = state["result"]
            if existing_result:
                for h in existing_result.hospitals:
                    h.status = HospitalStatus.CREATED_AND_ACTIVATED
                existing_result.batch_activated = True
                job_store.mark_done(job_id, existing_result)
        return

    total = len(state["original_rows"])
    completed_counter = [len(state["succeeded_rows"])]  # start from where we left off

    job_store.update_progress(
        job_id=job_id,
        total=total,
        completed=completed_counter[0],
        failed_count=len(rows_to_retry),
    )

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    tasks = [
        _create_one(row, batch_id, semaphore, job_id, total, completed_counter)
        for row in rows_to_retry
    ]
    new_results: list[HospitalResult] = await asyncio.gather(*tasks)

    # Merge new results with previous succeeded results
    all_results = state["succeeded_results"] + new_results
    succeeded = [r for r in all_results if r.hospital_id is not None]
    failed = [r for r in all_results if r.hospital_id is None]

    batch_activated = False
    if len(succeeded) == total:
        batch_activated = await activate_batch(batch_id)

    if batch_activated:
        for r in succeeded:
            r.status = HospitalStatus.CREATED_AND_ACTIVATED

    elapsed = round(time.monotonic() - start_time, 3)

    final_result = BulkCreateResponse(
        batch_id=batch_id,
        total_hospitals=total,
        processed_hospitals=len(succeeded),
        failed_hospitals=len(failed),
        processing_time_seconds=elapsed,
        batch_activated=batch_activated,
        hospitals=sorted(all_results, key=lambda r: r.row),
    )

    job_store.mark_done(job_id, final_result)