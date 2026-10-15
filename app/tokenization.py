"""离线 BPE、Unicode 安全边界和完整聊天输入估算。

借鉴 RAGFlow 的离线编码表及 FastGPT 的消息/工具计数。BPE 编码只对其
对应词表准确，聊天模板开销与跨模型计量仍是估算；真实用量以 provider 为准。
"""

import base64
import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

_DATA = Path(__file__).with_name("tokenizer_data")
_HASHES = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}
# 与官方 tiktoken 的 openai_public 编码定义一致；仅从本地加载 ranks。
_CL_PATTERN = (
    r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+"
    r"| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
)
_O_PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)


@lru_cache(maxsize=2)
def get_encoder(name="cl100k_base"):
    """缺表或校验失败即报错；不下载、不静默切换计量规则。"""
    if name not in _HASHES:
        raise ValueError(f"不支持的 tokenizer：{name}")
    data = (_DATA / f"{name}.tiktoken").read_bytes()
    if hashlib.sha256(data).hexdigest() != _HASHES[name]:
        raise ValueError(f"tokenizer 编码表校验失败：{name}")
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in data.splitlines() if line)
    }
    # 文档中的特殊标记始终视为普通文本，无需激活特殊 token。
    return tiktoken.Encoding(
        name=name,
        pat_str=_CL_PATTERN if name == "cl100k_base" else _O_PATTERN,
        mergeable_ranks=ranks,
        special_tokens={},
    )


def heuristic_count(text):
    ranges = (
        (0x3000, 0x30FF),
        (0x3400, 0x4DBF),
        (0x4E00, 0x9FFF),
        (0xAC00, 0xD7AF),
        (0xF900, 0xFAFF),
        (0xFF00, 0xFFEF),
    )
    cjk = sum(any(low <= ord(char) <= high for low, high in ranges) for char in text)
    return cjk + math.ceil((len(text) - cjk) / 4)


@dataclass(frozen=True)
class TokenCounter:
    name: str = "cl100k_base"

    def count(self, text: str) -> int:
        if self.name == "heuristic":
            return heuristic_count(text)
        return len(get_encoder(self.name).encode_ordinary(text))

    @property
    def version(self):
        return f"{self.name}:{_HASHES.get(self.name, 'chars-v1')}"

    def take(self, text, budget, *, suffix=False, format_text=None):
        """搜索可容纳的原文前/后缀。最终复检，不假定 BPE 计数严格单调。

        以 Unicode 字符为边界，允许少取，保证不损坏原文及不超限。
        """
        if budget <= 0:
            return ""
        format_text = format_text or (lambda value: value)

        def candidate(n):
            return text[len(text) - n :] if suffix else text[:n]

        # 指数探测限制局部编码量，避免对长文的每个剩余后缀反复编码。
        best, probe = 0, min(len(text), max(1, budget))
        while probe:
            if self.count(format_text(candidate(probe))) > budget:
                break
            best = probe
            if probe == len(text):
                return candidate(best)
            probe = min(len(text), probe * 2)
        left, right = best + 1, probe - 1
        while left <= right:
            mid = (left + right) // 2
            if self.count(format_text(candidate(mid))) <= budget:
                best, left = mid, mid + 1
            else:
                right = mid - 1
        result = candidate(best)
        while result and self.count(format_text(result)) > budget:
            best -= 1
            result = candidate(best)
        return result

    def messages(self, messages, tools=()):
        """覆盖角色、消息名、调用 ID/参数及工具 schema；模板常数仍属近似。"""
        total = 3
        for message in messages:
            payload = {"role": message.type, "content": message.content}
            for key in ("name", "tool_call_id", "tool_calls", "invalid_tool_calls"):
                value = getattr(message, key, None)
                if value:
                    payload[key] = value
            if message.additional_kwargs:
                payload["additional_kwargs"] = message.additional_kwargs
            total += 4 + self.count(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if tools:
            total += self.count(
                json.dumps(
                    [convert_to_openai_tool(tool) for tool in tools],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return total


DEFAULT_COUNTER = TokenCounter()


def count_tokens(text):
    """默认 BPE 计数；生产链路显式使用模型对应的 counter。"""
    return DEFAULT_COUNTER.count(text)


def embedding_text(row):
    return "\n".join(part for part in (row["name"], row["heading"], row["content"]) if part)


def embedding_limit(settings):
    limits = [
        n
        for n in (settings.embedding_max_input_tokens, settings.embedding_max_batch_tokens)
        if n > 0
    ]
    return max(1, int(min(limits) * settings.token_safety_ratio)) if limits else 0
