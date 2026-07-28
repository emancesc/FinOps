from __future__ import annotations
from typing import Optional
from datetime import datetime
import uuid
from pydantic import BaseModel, Field


class DocumentInput(BaseModel):
    doc_type: str = Field(..., pattern="^(HLD|LLD|PRE_MIGRATION_ASSESSMENT|MIGRATION_RESOURCE_LIST|TAGGING_STRATEGY|OTHER)$")
    file_name: str


class JobCreateRequest(BaseModel):
    account_id: str
    region: str
    tenant_id: str
    documents: list[DocumentInput] = []


class JobResponse(BaseModel):
    job_id: uuid.UUID
    account_id: str
    region: str
    tenant_id: str
    phase: str
    status_detail: Optional[str] = None
    progress_pct: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdvanceResponse(BaseModel):
    job_id: str
    previous_phase: str
    current_phase: str
    progress_pct: int
    queued: bool
