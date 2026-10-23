"""RAG 回答协议：严格校验来源关联，由服务端渲染引用。"""

import json
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from app.errors import ChatError

ANSWER_PROTOCOL = "rag-answer-v2"
SourceId = Annotated[StrictInt, Field(gt=0)]
AnswerStatus = Literal["answered", "insufficient_evidence"]
AnswerReason = Literal["missing_fact", "conflicting_sources", "no_retrieval_hits", "no_context_fit"]

ANSWER_INSTRUCTIONS = (
    "只输出一个 JSON 对象，不要 Markdown 代码围栏。对象必须包含 status、segments、reason。"
    '能完整回答时：{"status":"answered","segments":[{"text":"一段答案",'
    '"source_ids":[1]}],"reason":null}。每段必须有至少一个本轮 evidence.id，'
    "source_ids 为正整数数组，最多20个；最多32段，每段不超过6000字，总正文不超过24000字。"
    "示例不是资料。text 可含 Markdown，但不要手写引用标记；年份、数组下标等普通数字保持原文。"
    "服务端根据 source_ids 生成引用。只有本轮资料完整支持答案时才能回答。"
    '资料缺少所需事实时返回 {"status":"insufficient_evidence",'
    '"segments":[],"reason":"missing_fact"}；'
    "资料冲突且无法确定答案时将 reason 设为 conflicting_sources。不要编造、部分作答或强造引用。"
)


class AnswerSegment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(min_length=1, max_length=6000)
    source_ids: list[SourceId] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def nonempty_text(self):
        if not self.text.strip():
            raise ValueError("empty text")
        self.text.encode("utf-8", errors="strict")
        if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in self.text):
            raise ValueError("invalid control character")
        return self


class AnswerPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    status: AnswerStatus
    segments: list[AnswerSegment] = Field(max_length=32)
    reason: Literal["missing_fact", "conflicting_sources"] | None

    @model_validator(mode="after")
    def coherent_status(self):
        if self.status == "answered" and (not self.segments or self.reason is not None):
            raise ValueError("answered requires segments and no reason")
        if self.status == "insufficient_evidence" and (self.segments or self.reason is None):
            raise ValueError("insufficient requires reason and no segments")
        if sum(len(segment.text) for segment in self.segments) > 24000:
            raise ValueError("answer is too long")
        return self


def invalid(code, diagnostics=None):
    message = (
        "模型使用了无效的资料来源关联。"
        if code == "invalid_citations"
        else "模型未返回有效的结构化回答。"
    )
    return ChatError(code, message, 502, diagnostics=diagnostics)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("invalid JSON number")


def parse_answer(text, sources):
    if not isinstance(text, str) or len(text) > 64000:
        raise invalid("invalid_answer_schema")
    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        answer = AnswerPayload.model_validate(value)
    except json.JSONDecodeError as exc:
        raise invalid(
            "invalid_answer_schema",
            {
                "validation_kind": "invalid_json",
                "line": exc.lineno,
                "column": exc.colno,
                "offset": exc.pos,
            },
        ) from None
    except ValidationError as exc:
        code = (
            "invalid_citations"
            if any("source_ids" in error["loc"] for error in exc.errors())
            else "invalid_answer_schema"
        )
        # Never include raw model output, user text, or Pydantic input/context in diagnostics.
        raise invalid(
            code,
            {
                "validation_kind": "schema",
                "error_types": sorted({error["type"] for error in exc.errors()}),
                "error_count": exc.error_count(),
            },
        ) from None
    except (ValueError, TypeError, RecursionError):
        raise invalid("invalid_answer_schema") from None
    allowed = {source["citation_id"] for source in sources}
    for segment in answer.segments:
        if any(source_id not in allowed for source_id in segment.source_ids):
            raise invalid("invalid_citations")
        segment.source_ids = list(dict.fromkeys(segment.source_ids))
    return answer


def refusal(reason):
    texts = {
        "missing_fact": "现有资料不足以确认该问题的答案，请补充包含所需信息的资料。",
        "conflicting_sources": "检索到的资料存在冲突，暂时无法确认答案，请核对资料后重试。",
        "no_retrieval_hits": "未找到足够的知识库资料，暂时无法回答该问题。",
        "no_context_fit": (
            "已检索到资料，但当前上下文预算无法容纳可用片段，请缩小问题范围或调整资料预算。"
        ),
    }
    return {
        "answer": texts[reason],
        "answer_status": "insufficient_evidence",
        "answer_reason": reason,
        "answer_segments": [],
        "answer_protocol": ANSWER_PROTOCOL,
        "sources": [],
    }


def render_answer(payload, sources):
    if payload.status == "insufficient_evidence":
        return refusal(payload.reason)
    segments = [segment.model_dump() for segment in payload.segments]
    parts = []
    for segment in segments:
        text = segment["text"].rstrip()
        separator = "\n\n" if text.endswith(("```", "~~~")) else ""
        parts.append(text + separator + "".join(f"[{i}]" for i in segment["source_ids"]))
    used = {source_id for segment in segments for source_id in segment["source_ids"]}
    return {
        "answer": "\n\n".join(parts),
        "answer_status": "answered",
        "answer_reason": None,
        "answer_segments": segments,
        "answer_protocol": ANSWER_PROTOCOL,
        "sources": [source for source in sources if source["citation_id"] in used],
    }


def repair_instruction(code):
    if code not in {"invalid_answer_schema", "invalid_citations"}:
        raise ValueError("unknown repair code")
    return (
        f"\n\n上一次输出未通过服务端校验，错误码为 {code}。"
        "请依据相同的 evidence 和 question 重新输出符合上述协议的 JSON；"
        "不得使用本轮资料之外的编号，不得把格式错误当作资料不足。"
    )


def repair_reserve(counter):
    return (
        max(
            counter.count(repair_instruction(code))
            for code in ("invalid_answer_schema", "invalid_citations")
        )
        + 32
    )


def repair_messages(messages, code):
    copied = [message.model_copy(deep=True) for message in messages]
    copied[0] = copied[0].model_copy(update={"content": copied[0].text + repair_instruction(code)})
    return copied
