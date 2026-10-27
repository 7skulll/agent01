from copy import deepcopy

import pytest

from app.evaluation_models import EvalCase, cases_equal


def test_legacy_case_set_matches_validated_request_without_mutating_saved_json():
    saved = [
        {
            "query": "秋粮病虫害采取哪些措施？",
            "expected": "answerable",
            "judgment_scope": "partial",
            "relevant_file_ids": ["b", "a"],
            "required_facts": "11组，22省",
        }
    ]
    before = deepcopy(saved)
    requested = [EvalCase.model_validate(c).model_dump(mode="json") for c in saved]
    assert "reference_answer" not in saved[0]
    assert requested[0]["reference_answer"] == ""
    assert cases_equal(saved, requested)
    assert saved == before


@pytest.mark.parametrize(
    "changes",
    [
        {"query": "另一道问题"},
        {"reference_answer": "人工新写的参考答案"},
        {"relevant_file_ids": ["other"]},
        {"negative_file_ids": ["negative"]},
        {"judgment_scope": "full"},
        {"required_facts": "改变必要事实"},
        {"tags": ["changed"]},
        {
            "evidence_groups": [
                {
                    "fact": "事实",
                    "alternatives": [
                        {"file_id": "a", "content_hash": "a" * 64, "quote": "必要原文事实"}
                    ],
                }
            ]
        },
    ],
)
def test_substantive_case_edits_still_require_a_new_version(changes):
    original = {
        "query": "问题",
        "expected": "answerable",
        "relevant_file_ids": ["a"],
        "judgment_scope": "partial",
    }
    requested = EvalCase.model_validate({**original, **changes}).model_dump(mode="json")
    assert not cases_equal([original], [requested])


def test_reordered_cases_and_invalid_legacy_fields_do_not_match():
    cases = [{"query": "问题一"}, {"query": "问题二"}]
    assert not cases_equal(cases, list(reversed(cases)))
    for invalid in (None, "cases", [{"query": ""}], [{"query": "x", "unknown": True}]):
        assert not cases_equal(invalid, invalid)
