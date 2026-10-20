from copy import deepcopy

import pytest
from test_evaluation_v2 import experiment

from app.evaluation_comparison import compare
from app.evaluation_usage import actual_tokens


@pytest.mark.parametrize("value", [True, -1, 1.5, "3", None])
def test_actual_tokens_rejects_non_integer_or_invalid_counts(value):
    result = {"token_usage": [{"actual": {"input_tokens": value, "output_tokens": 2}}]}
    assert actual_tokens(result) is None


def test_zero_call_usage_is_consistent_in_quality_and_comparison():
    a = experiment()
    b = deepcopy(a)
    for row in (a, b):
        row["results"][0]["run"] = {"model_calls": 0}
    assert actual_tokens(a["results"][0]) == 0
    assert compare(a, b)["rows"][0]["tokens_delta"] == 0
    b["results"][0]["token_usage"] = [{"actual": {"input_tokens": 1.5, "output_tokens": 2}}]
    assert compare(a, b)["rows"][0]["tokens_delta"] is None
