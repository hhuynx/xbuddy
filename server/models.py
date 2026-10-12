"""Versioned data contracts used by storage, API and the upcoming harness."""
from __future__ import annotations

from enum import StrEnum
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Quality(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"
    CONFLICT = "conflict"
    UNAUTHORIZED = "unauthorized"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class ThreadCreate(Contract):
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(default="", max_length=4000)


class DocumentImport(Contract):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    title: str = Field(min_length=1,max_length=200)
    source_url: str = Field(min_length=1,max_length=1500)
    published_at: str = Field(min_length=1,max_length=100)
    text: str = Field(min_length=1,max_length=2_000_000)


class RunBudget(Contract):
    tool_attempts: int = Field(default=12, ge=0, le=100)
    model_calls: int = Field(default=4, ge=0, le=12)
    model_tokens: int = Field(default=100000, ge=0, le=500000)
    duration_seconds: int = Field(default=600, ge=10, le=3600)
    context_tokens: int = Field(default=12000, ge=2000, le=100000)


class RunCreate(Contract):
    goal: str = Field(min_length=1, max_length=4000)
    mode: Literal["manual", "agent"] = "manual"
    budget: RunBudget = Field(default_factory=RunBudget)


class InputResponse(Contract):
    question_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(min_length=1, max_length=4000)


class ApprovalDecision(Contract):
    decision: Literal["approve", "reject", "modify"]
    plan_version: int = Field(ge=1)
    parameters_fingerprint: str = Field(min_length=64, max_length=64)
    budget: RunBudget | None = None


class PlanUpdate(Contract):
    plan_version: int = Field(ge=1)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    plan: dict[str, Any]


class FollowUpCreate(Contract):
    goal: str = Field(min_length=1, max_length=4000)
    parent_artifact_id: str | None = Field(default=None, max_length=64)
    budget: RunBudget = Field(default_factory=RunBudget)


class ReportRevision(Contract):
    expected_version: int = Field(ge=1)
    note: str = Field(default="重新核对来源与计算", max_length=200)


class MemoryCreate(Contract):
    kind: Literal['preference', 'research_fact', 'hypothesis', 'open_question'] = 'preference'
    content: str = Field(default='', max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    symbols: list[str] = Field(default_factory=list, max_length=3)
    thread_id: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None
    source_artifact_id: str | None = Field(default=None, max_length=64)
    claim_index: int | None = Field(default=None, ge=0, le=500)

    @field_validator('expires_at')
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('日期必须包含时区')
        return value

    @field_validator('tags', 'symbols')
    @classmethod
    def short_labels(cls, values):
        if any(not v.strip() or len(v) > 40 for v in values):
            raise ValueError('标签或代码长度无效')
        return list(dict.fromkeys(v.strip() for v in values))

    @model_validator(mode='after')
    def source_required(self):
        if self.kind == 'research_fact' and (not self.source_artifact_id or self.claim_index is None):
            raise ValueError('研究事实必须选择已验证的报告结论')
        if self.kind != 'research_fact' and not self.content:
            raise ValueError('请填写记忆内容')
        return self


class MemoryUpdate(MemoryCreate):
    expected_version: int = Field(ge=1)


class MemoryDecision(Contract):
    decision: Literal['approve', 'reject']
    expected_version: int = Field(ge=1)
    content_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


class TaskCreate(Contract):
    title: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=30)


class ToolCallCreate(Contract):
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)
    task_id: str | None = None


class ThreadRecord(Contract):
    id: str
    title: str
    goal: str
    created_at: str
    updated_at: str


class RunRecord(Contract):
    id: str
    thread_id: str
    goal: str
    mode: str
    status: str
    schema_version: int
    plan_version: int
    config: dict[str, Any]
    attempts_used: int
    created_at: str
    updated_at: str


class TaskRecord(Contract):
    id: str
    run_id: str
    title: str
    tool: str
    arguments: dict[str, Any]
    depends_on: list[str]
    status: str
    created_at: str
    updated_at: str


class ToolCallRecord(Contract):
    id: str
    run_id: str
    task_id: str | None
    tool: str
    version: str
    parameters: dict[str, Any]
    fingerprint: str
    idempotency_key: str
    status: str
    quality: Quality | None
    attempts: int
    elapsed_ms: int | None
    result: dict[str, Any] | None
    created_at: str
    finished_at: str | None
    reused: bool = False


class EvidenceContract(Contract):
    schema_version: int = 1
    provider: str
    tool: str
    endpoint: str
    parameters: dict[str, Any]
    request_id: str | None
    fetched_at: str
    as_of: str | None
    quality: Quality
    records: list[dict[str, Any]]
    field_map: dict[str, Any]
    units: dict[str, str]
    method: dict[str, Any]
    issues: list[str]
    trust: Literal["external_untrusted", "derived"] = "external_untrusted"


class ClaimContract(Contract):
    text: str
    kind: Literal["fact", "inference", "uncertain"]
    evidence_ids: list[str]
    derived_metric_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    as_of: str | None = None


class ArtifactContract(Contract):
    kind: str
    version: int = Field(ge=1)
    claim_ids: list[str]
    evidence_ids: list[str]
    validation_status: Literal["pending", "passed", "failed"] = "pending"


class ApprovalContract(Contract):
    plan_version: int = Field(ge=1)
    action: str
    parameters_fingerprint: str
    reason: str
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"


class MemoryContract(Contract):
    kind: Literal["preference", "research_fact", "hypothesis", "open_question"]
    content: str
    evidence_ids: list[str]
    valid_as_of: str | None
    expires_at: str | None
    write_basis: str


class EventContract(Contract):
    event_type: str
    payload: dict[str, Any]
    created_at: str
