"""FastGPT 文本分词移植：Jieba 普通模式/HMM、相同清洗及停用词。"""

import json
import re
from pathlib import Path

import rjieba

TERMS_VERSION = "fastgpt-jieba-rs-0.1.13-hmm-v1"
_STOP_WORDS = frozenset(
    json.loads((Path(__file__).parent / "term_data" / "stopwords.json").read_text())
)
_MARKDOWN_URL = re.compile(r"[#*`_~>\[\](){}|]|\S*https?\S*")
_WIDE_PUNCTUATION = re.compile(r"[\u3000-\u303f\uff00-\uffef]")


def document_terms(text):
    """索引与查询共用普通 cut，保留顺序/词频；不再加双字词项或自定义词表。"""
    text = _MARKDOWN_URL.sub("", text).strip()
    return [
        token
        for word in rjieba.cut(text, True)
        if (token := _WIDE_PUNCTUATION.sub("", word).strip()) and token not in _STOP_WORDS
    ]


def search_terms(text):
    return list(dict.fromkeys(document_terms(text)))
