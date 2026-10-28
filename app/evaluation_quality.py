"""Small-sample quality reporting. Human judgments and text coverage stay separate."""

from collections import Counter

from app.evaluation_usage import actual_tokens

# Answer-quality metrics are derived on read from the raw human-review fields.
# The formulas are intentionally strict: only an explicit positive judgment counts;
# pending, "na", missing reviews and failed runs never become a pass.  Derived values
# are never written back to the stored evaluation result.
ANSWER_METRIC_SPECS = (
    {
        "key": "faithfulness",
        "label": "忠实度",
        "field": "unsupported_claims",
        "positive": "no",
        "values": ("yes", "no"),
        "definition": "已审核回答中，不存在无资料支持表述的比例（unsupported_claims=no）。",
    },
    {
        "key": "answer_relevancy",
        "label": "答案相关性",
        "field": "answer_relevancy",
        "positive": "relevant",
        "values": ("relevant", "partial", "irrelevant"),
        "definition": (
            "已审核回答中，直接回应问题的比例（answer_relevancy=relevant）；"
            "部分相关不计为严格相关。"
        ),
    },
    {
        "key": "correctness",
        "label": "正确性",
        "field": "verdict",
        "positive": "pass",
        "values": ("pass", "partial", "fail"),
        "definition": "已审核回答中，人工总评为通过的比例（verdict=pass）；部分正确不计为通过。",
    },
    {
        "key": "completeness",
        "label": "完整性",
        "field": "fact_coverage",
        "positive": "full",
        "values": ("full", "partial", "none"),
        "definition": (
            "已审核回答中，必要事实完整覆盖的比例（fact_coverage=full）；"
            "部分覆盖不计为完整。"
        ),
    },
)


def review_metric_score(result, spec):
    """Return 1/0 only for an explicit human judgment; pending/na stay missing."""
    value = result.get("review", {}).get(spec["field"])
    if value not in spec["values"]:
        return None
    return 1.0 if value == spec["positive"] else 0.0


def _answer_metric(eligible, spec):
    # Rows written before a dimension existed have no key at all; they are unaudited,
    # not "not applicable", so they must be counted as pending instead of vanishing.
    counts = Counter(
        r.get("review", {}).get(spec["field"]) or "pending" for r in eligible
    )
    denominator = sum(counts[value] for value in spec["values"])
    metric = {
        "key": spec["key"],
        "label": spec["label"],
        "definition": spec["definition"],
        "source_field": spec["field"],
        "positive_value": spec["positive"],
        "counts": {value: counts[value] for value in spec["values"]},
        "numerator": counts[spec["positive"]],
        "denominator": denominator,
        "judged": denominator,
        "pending": counts["pending"],
        "na": counts["na"],
        "value": counts[spec["positive"]] / denominator if denominator else None,
    }
    if spec["key"] == "correctness":
        # Reported for diagnosis only; it never replaces the strict pass rate.
        metric["acceptable"] = counts["pass"] + counts["partial"]
        metric["acceptable_rate"] = (
            (counts["pass"] + counts["partial"]) / denominator if denominator else None
        )
    return metric


def generation_summary(results):
    """Aggregate optional RAGAS LLM-as-judge scores. Never fills missing values with zero."""
    judged_rows = [r for r in results if isinstance(r.get("judge"), dict)]
    attempted = [r for r in results if isinstance(r.get("judge"), dict) or r.get("judge_error")]
    keys = ("faithfulness", "answer_relevancy", "answer_correctness", "completeness")
    metrics = {}
    for key in keys:
        values = [
            float(row["judge"][key])
            for row in judged_rows
            if isinstance(row["judge"].get(key), (int, float))
            and not isinstance(row["judge"].get(key), bool)
        ]
        metrics[key] = {
            "value": sum(values) / len(values) if values else None,
            "judged": len(values),
            "missing": len(attempted) - len(values),
            "source": "ragas",
        }
    return {
        "available": bool(judged_rows),
        "judged_queries": len(judged_rows),
        "attempted_queries": len(attempted),
        "metrics": metrics,
        "errors": sum(bool(r.get("judge_error")) for r in results),
    }


def quality_summary(results):
    eligible = [r for r in results if r["status"] == "completed" and r.get("run")]
    verdicts = Counter(r.get("review", {}).get("verdict", "pending") for r in eligible)
    dimensions = {}
    for field, values, positive in [
        ("fact_coverage", ("full", "partial", "none"), "full"),
        ("unsupported_claims", ("yes", "no"), "yes"),
        ("conditions", ("pass", "fail"), "pass"),
        ("answer_relevancy", ("relevant", "partial", "irrelevant"), "relevant"),
        ("citation_support", ("full", "partial", "none"), "full"),
        ("refusal", ("correct", "incorrect"), "correct"),
    ]:
        counts = Counter(r.get("review", {}).get(field, "pending") for r in eligible)
        denominator = sum(counts[value] for value in values)
        dimensions[field] = {
            "counts": dict(counts),
            "judged": denominator,
            "pending": counts["pending"],
            "na": counts["na"],
            "rate": counts[positive] / denominator if denominator else None,
        }
    answer_metrics = {
        spec["key"]: _answer_metric(eligible, spec) for spec in ANSWER_METRIC_SPECS
    }
    usages = [
        actual_tokens(r)
        for r in results
        if not r.get("reused_from") and r["status"] in ("completed", "failed", "cancelled")
    ]
    causes = Counter(
        c
        for r in results
        for c in set(
            r.get("evidence", {}).get("causes", []) + r.get("review", {}).get("causes", [])
        )
    )
    return {
        "total": len(results),
        "answer_queries": len(eligible),
        "verdicts": dict(verdicts),
        "pass_rate": verdicts["pass"] / (len(eligible) - verdicts["pending"])
        if len(eligible) > verdicts["pending"]
        else None,
        "metrics": answer_metrics,
        "dimensions": dimensions,
        "causes": dict(causes),
        "reused": sum(bool(r.get("reused_from")) for r in results),
        "usage": {
            "known_tokens": sum(v for v in usages if v is not None),
            "known_queries": sum(v is not None for v in usages),
            "unknown_queries": sum(v is None for v in usages),
            "new_model_calls": sum(
                r.get("run", {}).get("model_calls", 0) for r in results if not r.get("reused_from")
            ),
        },
    }


def markdown_report(row):
    def cell(value):
        return (
            str(value)
            .replace("|", "\\|")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def pct(value):
        return "未评分" if value is None else f"{value:.1%}"

    request, quality = row["request"], row["quality"]
    lines = [
        "# RAG 小样本验收报告",
        "",
        f"实验：{row['evaluation_id']} · 状态：{row['status']}",
        f"知识库：{cell(request['knowledge_base_id'])} · {request['kind']} / {request['mode']}"
        f" · K={request['k']}",
        f"资料与配置指纹：{row['snapshot']['fingerprint']}",
        f"案例集元数据：{cell(row['snapshot'].get('case_set', '未绑定'))}",
        f"实验参数：{cell(request.get('options', {}))}",
        f"业务标签筛选：{cell(row.get('filter_tag') or '全部')} · 复用成功题：{quality['reused']}",
        "",
        "## 检索指标",
        "",
        "K 表示返回片段内去重后的文件排名。片段上限与问答候选配置一致；"
        "文件命中不等于必要事实进入上下文。部分标注覆盖率不是完整召回率或其下界。",
        "",
        "| 指标 | 值 |",
        "|---|---|",
    ]
    lines += [
        f"| {name}@{request['k']} | {pct(value)} |"
        for name, value in row["summary"]["metrics"].items()
    ]
    lines += [
        "",
        "完整标注计分题："
        f"{row['summary']['metric_queries']}（失败 "
        f"{row['summary'].get('labelled_failures', 0)} 题，"
        f"其中 {row['summary'].get('retrieval_scored_failures', 0)} 题按已保留的检索结果计分；"
        "无可用检索结果的失败题计零）；小样本不推断生产准确率或显著性。",
        "",
        "## 人工回答质量",
        "",
        f"总评：{cell(quality['verdicts'])}；通过率 {pct(quality['pass_rate'])}。"
        "待审核/不适用不充当通过。",
    ]
    lines += [
        f"- {name}：{pct(value['rate'])}，已审核 {value['judged']}，"
        f"待审核 {value['pending']}，不适用 {value['na']}。"
        for name, value in quality["dimensions"].items()
    ]
    lines += [
        "",
        "## 回答质量指标（严格口径）",
        "",
        "以下数值在读取时由人工审核原始字段直接推导，不修改原始记录；"
        "待审核、不适用、缺失和服务失败都不计为通过。",
        "",
        "| 指标 | 值 | 计数 | 已审核分母 | 待审核 | 不适用 |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {metric['label']} | {pct(metric['value'])} | "
        f"{metric['numerator']}/{metric['denominator']} | {metric['judged']} | "
        f"{metric['pending']} | {metric['na']} |"
        for metric in quality.get("metrics", {}).values()
    ]
    lines += [
        "",
        "忠实度=unsupported_claims 为 no 的比例；答案相关性=answer_relevancy 为 relevant 的比例；"
        "正确性=verdict 为 pass 的比例；完整性=fact_coverage 为 full 的比例。"
        "部分正确或部分覆盖不会被计入严格通过。",
        "",
    ]
    generation = row.get("generation") or {}
    if generation.get("available"):
        lines += [
            "## RAGAS 生成层评分",
            "",
            "RAGAS 为可选的 LLM-as-judge 语义评分；缺失分数不补零，也不覆盖人工审核。",
            "",
            "| 指标 | 均值 | 已评 | 缺失 |",
            "|---|---|---|---|",
        ]
        labels = {
            "faithfulness": "忠实度",
            "answer_relevancy": "答案相关性",
            "answer_correctness": "答案正确性",
            "completeness": "完整性",
        }
        for key, label in labels.items():
            metric = generation["metrics"][key]
            lines.append(
                f"| {label} | {pct(metric['value'])} | {metric['judged']} | {metric['missing']} |"
            )
        lines += [""]
    lines += [
        "",
        f"实际用量（仅本次执行已知部分）：{cell(quality['usage'])}",
        f"必要证据完整进入上下文：{row['summary'].get('context_evidence_complete', 0)}/"
        f"{row['summary'].get('context_evidence_queries', 0)}；"
        f"正确拒答（已完成）：{row['summary'].get('refusal_correct', 0)}/"
        f"{row['summary'].get('refusal_total', 0)}；"
        f"正确拒答（含失败）：{row['summary'].get('refusal_correct', 0)}/"
        f"{row['summary'].get('refusal_attempted', 0)}。",
        "",
        "## 逐题结果与问题",
        "",
        "| 问题 | 状态 | 人工总评 | 归因/备注 |",
        "|---|---|---|---|",
    ]
    for result in row["results"]:
        if row.get("filter_tag") and row["filter_tag"] not in result["case"].get("tags", []):
            continue
        review = result.get("review", {})
        issues = result.get("evidence", {}).get("causes", []) + review.get("causes", [])
        reused = "（复用）" if result.get("reused_from") else ""
        failure = (result.get("error") or {}).get("message", "")
        lines.append(
            f"| {cell(result['case']['query'])} | {cell(result['status'])}{reused} | "
            f"{cell(review.get('verdict', 'pending'))} | {cell(', '.join(issues))} "
            f"{cell(review.get('notes', ''))} {cell(failure)} |"
        )
    lines += [
        "",
        "## 改进与验收边界",
        "",
        "先复核失败与未审核案例，再按归因检查召回、重排和上下文裁剪。原文包含不等于语义支持，人工通过不等于完整业务验收。",
        "未核验业务案例、草稿案例集与项目文档样例均不能作为生产签收结论。",
        "",
    ]
    return "\n".join(lines)
