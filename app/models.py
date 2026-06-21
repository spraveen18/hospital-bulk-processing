# app/models.py

import uuid
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class HospitalStatus(str, Enum):
    CREATED_AND_ACTIVATED = "created_and_activated"
    FAILED = "failed"


class HospitalRow(BaseModel):
    row: int                          
    name: str
    address: str
    phone: Optional[str] = None       


class HospitalResult(BaseModel):
    # Represents the outcome for one hospital in the final response
    row: int
    hospital_id: Optional[int] = None # None if creation failed
    name: str
    status: HospitalStatus
    error: Optional[str] = None       # populated only on failure


class BulkCreateResponse(BaseModel):
    # The exact response shape the spec requires
    batch_id: str
    total_hospitals: int
    processed_hospitals: int
    failed_hospitals: int
    processing_time_seconds: float
    batch_activated: bool
    hospitals: list[HospitalResult]