"""Actual token totals are known only with complete, non-negative integer usages."""


def actual_tokens(result):
    events = result.get("token_usage", [])
    if not events:
        return 0 if (result.get("run") or {}).get("model_calls") == 0 else None
    if any(
        not e.get("actual")
        or any(
            type(e["actual"].get(k)) is not int or e["actual"][k] < 0
            for k in ("input_tokens", "output_tokens")
        )
        for e in events
    ):
        return None
    return sum(e["actual"]["input_tokens"] + e["actual"]["output_tokens"] for e in events)
