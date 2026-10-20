"""Validated inputs for the evaluation API."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import EvaluationOptions

FileID = Annotated[str, Field(min_length=1, max_length=128)]


class EvidenceAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    file_id: FileID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote: str = Field(min_length=4, max_length=2000)
    page: int | None = Field(default=None, ge=1)


class EvidenceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    fact: str = Field(min_length=1, max_length=500)
    alternatives: list[EvidenceAnchor] = Field(min_length=1, max_length=5)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    expected: Literal["unlabelled", "answerable", "unanswerable"] = "unlabelled"
    relevant_file_ids: list[FileID] = Field(default_factory=list, max_length=100)
    required_facts: str = Field(default="", max_length=2000)
    judgment_scope: Literal["full", "partial"] = "full"
    negative_file_ids: list[FileID] = Field(default_factory=list, max_length=100)
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=10
    )
    evidence_groups: list[EvidenceGroup] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def coherent(self):
        self.relevant_file_ids = sorted(set(self.relevant_file_ids))
        if (self.expected == "answerable") != bool(self.relevant_file_ids):
            raise ValueError("有答案标注必须选择相关文件；未标注和应拒答案例不填写相关文件。")
        self.negative_file_ids = sorted(set(self.negative_file_ids))
        if set(self.negative_file_ids) & set(self.relevant_file_ids):
            raise ValueError("同一文件不能同时标注正相关和负相关。")
        if self.evidence_groups and self.expected != "answerable":
            raise ValueError("必要证据用于有答案案例。")
        if any(
            a.file_id not in self.relevant_file_ids
            for g in self.evidence_groups
            for a in g.alternatives
        ):
            raise ValueError("证据文件必须属于正相关文件。")
        return self


class EvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: UUID
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    kind: Literal["retrieval", "rag"] = "retrieval"
    mode: Literal["keyword", "vector", "hybrid"] = "hybrid"
    k: int = Field(default=5, ge=1, le=20)
    file_ids: list[FileID] | None = Field(default=None, max_length=100)
    cases: list[EvalCase] = Field(min_length=1, max_length=20)
    case_set_id: UUID | None = None
    options: EvaluationOptions = Field(default_factory=EvaluationOptions)
    retry_of: UUID | None = None
    retry_unknown_confirmed: bool = False


class CaseSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=10
    )
    cases: list[EvalCase] = Field(min_length=1, max_length=20)

    split: Literal["development", "acceptance"] = "development"
    review_status: Literal["draft", "verified"] = "draft"
    frozen: bool = False

    @model_validator(mode="after")
    def frozen_requires_verified(self):
        if self.frozen and (
            self.review_status != "verified"
            or any(
                c.expected == "unlabelled"
                or c.judgment_scope != "full"
                or (c.expected == "answerable" and not c.evidence_groups)
                for c in self.cases
            )
        ):
            raise ValueError("冻结案例集需要人工核验、完整标注及有答案题的必要原文。")
        return self


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    confirm_unknown_calls: bool = False


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["pending", "pass", "partial", "fail"] = "pending"
    notes: str = Field(default="", max_length=2000)
    fact_coverage: Literal["pending", "full", "partial", "none"] = "pending"
    unsupported_claims: Literal["pending", "yes", "no"] = "pending"
    conditions: Literal["pending", "pass", "fail", "na"] = "pending"
    citation_support: Literal["pending", "full", "partial", "none", "na"] = "pending"
    refusal: Literal["pending", "correct", "incorrect", "na"] = "pending"
    causes: list[
        Literal[
            "annotation_gap", "parsing", "retrieval", "rerank", "assembly", "generation", "unknown"
        ]
    ] = Field(default_factory=list, max_length=7)
