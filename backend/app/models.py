from typing import Literal

from pydantic import BaseModel, Field, field_validator

IncidentKind = Literal["api_errors", "database_contention", "invalid_configuration", "unknown"]
Decision = Literal["approve", "reject", "request_changes"]


class IncidentCreate(BaseModel):
    title: str = Field(min_length=5, max_length=140)
    description: str = Field(min_length=20, max_length=4000)
    service: str = Field(min_length=2, max_length=80)
    severity: Literal["low", "medium", "high", "critical"] = "high"

    @field_validator("title", "description", "service")
    @classmethod
    def trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value


class ReviewRequest(BaseModel):
    decision: Decision
    change_request: str | None = Field(default=None, max_length=1000)

    @field_validator("change_request")
    @classmethod
    def validate_change_request(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
        return value or None


class InvestigationCreate(BaseModel):
    incident_id: str = Field(min_length=3, max_length=100)


class ToolRecord(BaseModel):
    id: str
    source: str
    timestamp: str
    summary: str
    detail: str
    signal: str | None = None


class EventOut(BaseModel):
    id: int
    run_id: str
    kind: str
    message: str
    payload: dict = Field(default_factory=dict)
    created_at: str
