"""Isolated RAGAS judge.

Protocol:
  stdin  -> {"records": [...], "llm": {...}, "embeddings": {...}}
  stdout -> {"scores": [...], "ragas_version": "..."}

The main service only knows this JSON protocol; it never imports ragas or its
heavy transitive dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

KEYS = ("faithfulness", "answer_relevancy", "answer_correctness", "completeness")


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def pick(row, *names):
    for name in names:
        for key, value in row.items():
            if str(key).lower() == name:
                result = number(value)
                if result is not None:
                    return result
    return None


def score_rows(result):
    scores = getattr(result, "scores", None)
    if isinstance(scores, list):
        return scores
    return result.to_pandas().to_dict("records")


def build_models(config):
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    llm = ChatOpenAI(
        model=config["llm"]["model"],
        api_key=config["llm"]["api_key"],
        base_url=config["llm"].get("base_url") or None,
        temperature=config["llm"].get("temperature", 0),
        max_tokens=config["llm"].get("max_tokens", 1024),
        max_retries=0,
    )
    embeddings = OpenAIEmbeddings(
        model=config["embeddings"]["model"],
        api_key=config["embeddings"]["api_key"],
        base_url=config["embeddings"].get("base_url") or None,
        check_embedding_ctx_length=False,
        max_retries=0,
    )
    return llm, embeddings


def evaluate_payload(payload):
    import ragas
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_correctness, answer_relevancy, faithfulness

    try:
        from ragas.metrics import AspectCritic

        completeness = AspectCritic(
            name="completeness",
            definition=(
                "The response covers every material fact in the reference answer. "
                "Omissions of required facts make it incomplete."
            ),
            strictness=1,
        )
    except Exception:
        completeness = None

    llm, embeddings = build_models(payload)
    llm = LangchainLLMWrapper(llm)
    embeddings = LangchainEmbeddingsWrapper(embeddings)
    records = payload["records"]
    output = [{key: None for key in KEYS} for _ in records]

    def run(rows, metrics):
        samples = [
            SingleTurnSample(
                user_input=row["question"],
                response=row["answer"],
                retrieved_contexts=row.get("contexts") or [],
                **({"reference": row["reference"]} if row.get("reference") else {}),
            )
            for row in rows
        ]
        result = evaluate(
            EvaluationDataset(samples=samples),
            metrics=metrics,
            llm=llm,
            embeddings=embeddings,
            raise_exceptions=False,
        )
        return score_rows(result)

    base_rows = [row for row in records if row.get("contexts")]
    if base_rows:
        scores = run(base_rows, [faithfulness, answer_relevancy])
        for row, score in zip(base_rows, scores, strict=True):
            output[row["_slot"]]["faithfulness"] = pick(score, "faithfulness")
            output[row["_slot"]]["answer_relevancy"] = pick(
                score, "answer_relevancy", "response_relevancy"
            )

    reference_rows = [row for row in base_rows if row.get("reference")]
    if reference_rows:
        metrics = [answer_correctness]
        if completeness is not None:
            metrics.append(completeness)
        scores = run(reference_rows, metrics)
        for row, score in zip(reference_rows, scores, strict=True):
            output[row["_slot"]]["answer_correctness"] = pick(score, "answer_correctness")
            if completeness is not None:
                output[row["_slot"]]["completeness"] = pick(
                    score, "completeness", "aspect_critic"
                )

    return {
        "scores": output,
        "ragas_version": getattr(ragas, "__version__", "unknown"),
        "worker": "tools/ragas-worker",
    }


def main():
    parser = argparse.ArgumentParser(description="Isolated RAGAS judge worker")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        import ragas

        print(json.dumps({"ragas_version": getattr(ragas, "__version__", "unknown")}))
        return
    try:
        payload = json.load(sys.stdin)
        for index, row in enumerate(payload.get("records") or []):
            row["_slot"] = index
        print(json.dumps(evaluate_payload(payload), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - worker boundary
        message = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(message, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
