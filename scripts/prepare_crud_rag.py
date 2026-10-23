"""Download, prepare and import a reproducible, review-pending CRUD-RAG QA subset."""

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

import httpx

from app.evaluation_models import CaseSetRequest

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "1aace383994e1f68efa12cf2a8e2dadfb4102ceb"
BLOB = "2dd57307cc39efca886d8ed955d4cade84fe9887"
SHA256 = "b199fb281ac526c1c8da7364cad7ea22b7f6fe1d4167e511edbdda8ad3bd943a"
SOURCE = f"https://github.com/IAAR-Shanghai/CRUD_RAG/blob/{COMMIT}"
TASKS = {f"questanswer_{n}doc" + ("s" if n > 1 else ""): n for n in (1, 2, 3)}
CACHE = ROOT / "data/crud-rag"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def normalized(value):
    return re.sub(r"\s+", " ", value).strip()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_upstream(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("上游文件校验失败；需要固定版本的原始 split_merged.json。")
    return json.loads(raw)


def build_subset(data, *, per_task=20, distractors=100, seed=22):
    """Group all QA by event, identical text and identical question before splitting."""
    if per_task < 2 or distractors < 0:
        raise ValueError("每类至少 2 题，干扰文档数不能为负。")
    parents, owners, records = {}, {}, []

    def find(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]

    for task, count in TASKS.items():
        for row in data[task]:
            fields = ["ID", "questions", "answers", *[f"news{n}" for n in range(1, count + 1)]]
            if any(not isinstance(row.get(k), str) or not row[k].strip() for k in fields):
                raise ValueError(f"上游 {task} 有缺失字段。")
            key = row["ID"]
            find(key)
            tokens = ["event:" + normalized(row.get("event", key))]
            tokens += ["question:" + normalized(row["questions"])]
            tokens += [
                "document:" + digest(normalized(row[f"news{n}"])) for n in range(1, count + 1)
            ]
            for token in tokens:
                other = owners.setdefault(token, key)
                a, b = sorted((find(key), find(other)))
                parents[b] = a
            records.append((task, row))

    selected, counts, seen_questions = [], Counter(), set()
    acceptance = max(1, per_task * 3 // 10)
    quotas = {"development": per_task - acceptance, "acceptance": acceptance}
    records.sort(key=lambda item: digest(f"{seed}:{item[0]}:{item[1]['ID']}"))
    for task, row in records:
        group = find(row["ID"])
        split = "acceptance" if int(digest(f"{seed}:{group}")[:8], 16) % 10 < 3 else "development"
        question = normalized(row["questions"])
        if counts[task, split] >= quotas[split] or question in seen_questions:
            continue
        # Existing API limits are checked without silently truncating questions/answers.
        if len(row["questions"].strip()) > 4000 or len(row["answers"].strip()) > 1980:
            continue
        counts[task, split] += 1
        seen_questions.add(question)
        selected.append((task, row, group, split))
    if any(counts[t, s] != n for t in TASKS for s, n in quotas.items()):
        raise ValueError("无法满足分组抽样配额，请降低 per-task。")

    documents, cases = {}, []

    def add_document(text, event_id, origin, role):
        # Keep original bytes. The normalized key is only used to detect split leakage.
        doc_id = "crud-" + digest(text)
        documents.setdefault(
            doc_id,
            {
                "document_id": doc_id,
                "content_hash": digest(text),
                "text": text,
                "origins": [],
                "role": role,
            },
        )["origins"].append({"event_id": event_id, "field": origin})
        return doc_id

    for task, row, group, split in selected:
        doc_ids = list(
            dict.fromkeys(
                add_document(row[f"news{n}"], row["ID"], f"{task}.news{n}", "source")
                for n in range(1, TASKS[task] + 1)
            )
        )
        cases.append(
            {
                "case_id": "crud-" + digest(task + ":" + row["ID"] + ":" + row["questions"])[:24],
                "upstream_id": row["ID"],
                "task": task,
                "group_id": group,
                "split": split,
                "query": row["questions"].strip(),
                "reference_answer": row["answers"].strip(),
                "known_source_document_ids": doc_ids,
                "judgment_scope": "partial",
                "review_status": "draft",
                "evidence_groups": [],
            }
        )
    selected_groups = {group for _, _, group, _ in selected}
    text_keys = {digest(normalized(d["text"])) for d in documents.values()}
    extra = sorted(data["event_summary"], key=lambda r: digest(f"{seed}:extra:{r['ID']}"))
    added = 0
    for row in extra:
        if added == distractors:
            break
        key = digest(normalized(row["text"]))
        if find(row["ID"]) in selected_groups or key in text_keys:
            continue
        add_document(row["text"], row["ID"], "event_summary.text", "distractor_candidate")
        text_keys.add(key)
        added += 1
    if added != distractors:
        raise ValueError("不重复的干扰文档不足。")
    return {
        "schema_version": 1,
        "name": f"CRUD-RAG QA local subset seed-{seed}",
        "source": {
            "repository": "https://github.com/IAAR-Shanghai/CRUD_RAG",
            "commit": COMMIT,
            "path": "data/crud_split/split_merged.json",
            "sha256": SHA256,
            "url": SOURCE + "/data/crud_split/split_merged.json",
        },
        "sampling": {
            "seed": seed,
            "per_task": per_task,
            "distractors": distractors,
            "split_method": "event/document/question connected components",
        },
        "notice": "本地问答子集；非八万篇全库官方成绩。配套新闻不是完整 qrels，待人工核验。"
        "干扰候选未经逐题负相关判定；不生成无答案标签或伪造事实证据。",
        "cases": sorted(cases, key=lambda c: (c["split"], c["task"], c["case_id"])),
        "documents": sorted(documents.values(), key=lambda d: d["document_id"]),
    }


def write_subset(manifest, output):
    # A manifest is immutable once prepared; avoid mixing documents from different runs.
    output.mkdir(parents=True, exist_ok=False)
    (output / "documents").mkdir()
    for document in manifest["documents"]:
        path = "documents/" + document["document_id"] + ".txt"
        (output / path).write_bytes(document.pop("text").encode("utf-8"))
        document["path"] = path
    save_json(output / "manifest.json", manifest)
    counts = Counter((c["split"], c["task"]) for c in manifest["cases"])
    lines = [
        "# CRUD-RAG 本地子集",
        "",
        manifest["notice"],
        "",
        f"原始版本：{COMMIT}；随机种子：{manifest['sampling']['seed']}",
        "",
        f"问题：{len(manifest['cases'])}；文档：{len(manifest['documents'])}",
        "",
        "| 集合 | 任务 | 数量 |",
        "|---|---|---|",
    ]
    lines += [f"| {split} | {task} | {n} |" for (split, task), n in sorted(counts.items())]
    lines += [
        "",
        "全部为未审核、部分标注。答案只在 manifest 中，documents 中仅含原始新闻。",
        "新闻是历史资料，回答以原文时间为准；不得解释为当前事实。",
        "",
    ]
    (output / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def validate_bundle(folder):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    contents = {}
    for doc in manifest["documents"]:
        path = (folder / doc["path"]).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError("文档路径超出数据目录。")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != doc["content_hash"]:
            raise ValueError(f"文档哈希不一致：{doc['document_id']}")
        if doc["document_id"] in contents:
            raise ValueError("文档 ID 重复。")
        contents[doc["document_id"]] = raw
    ids, questions, groups, split_docs = set(), set(), {}, {}
    for case in manifest["cases"]:
        if case["case_id"] in ids or normalized(case["query"]) in questions:
            raise ValueError("案例 ID 或问题重复。")
        ids.add(case["case_id"])
        questions.add(normalized(case["query"]))
        split = case["split"]
        if split not in ("development", "acceptance"):
            raise ValueError("集合类型无效。")
        if groups.setdefault(case["group_id"], split) != split:
            raise ValueError("同事件跨集合。")
        if not case["known_source_document_ids"]:
            raise ValueError("缺少配套新闻。")
        for doc_id in case["known_source_document_ids"]:
            key = digest(normalized(contents[doc_id].decode("utf-8")))
            if split_docs.setdefault(key, split) != split:
                raise ValueError("相同新闻跨集合。")
    fake_bindings = {key: {"file_id": key} for key in contents}
    for start in range(0, len(manifest["cases"]), 20):
        CaseSetRequest.model_validate(
            {
                "knowledge_base_id": "validation-only",
                "name": "CRUD validation",
                "cases": bound_cases(manifest["cases"][start : start + 20], fake_bindings),
            }
        )
    return manifest, contents


def bound_cases(cases, bindings):
    return [
        {
            "query": c["query"],
            "expected": "answerable",
            "judgment_scope": "partial",
            "relevant_file_ids": [bindings[d]["file_id"] for d in c["known_source_document_ids"]],
            "negative_file_ids": [],
            "required_facts": "上游参考答案（待核验）：" + c["reference_answer"],
            "evidence_groups": [],
            "tags": ["crud-rag", c["task"], "review-pending", c["case_id"]],
        }
        for c in cases
    ]


def revise_bundle(folder, output, reviews_path):
    """Create a new local version; preserve upstream references and historical runs."""
    manifest, contents = validate_bundle(folder)
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    if reviews.get("schema_version") != 1:
        raise ValueError("复核文件版本无效。")
    cases = {case["case_id"]: case for case in manifest["cases"]}
    excluded, seen = [], set()
    for review in reviews["reviews"]:
        case_id = review["case_id"]
        if case_id not in cases or case_id in seen:
            raise ValueError("复核案例不存在或重复。")
        if review["action"] != "exclude" or not review.get("reason"):
            raise ValueError("只支持有明确原因的排除，不自动升级标注为完整或已核验。")
        seen.add(case_id)
        excluded.append({"case": cases[case_id], "review": review})
    manifest["cases"] = [c for c in manifest["cases"] if c["case_id"] not in seen]
    manifest["excluded_cases"] = excluded
    manifest["review_overlay"] = {
        "sha256": hashlib.sha256(reviews_path.read_bytes()).hexdigest(),
        "parent_manifest_sha256": hashlib.sha256(
            (folder / "manifest.json").read_bytes()
        ).hexdigest(),
    }
    for doc in manifest["documents"]:
        doc["text"] = contents[doc["document_id"]].decode("utf-8")
    write_subset(manifest, output)
    validate_bundle(output)
    return len(excluded)


async def import_bundle(folder, url, kb_id, *, index=False, token=""):
    manifest, contents = validate_bundle(folder)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        base_url=url.rstrip("/") + "/", headers=headers, timeout=60
    ) as client:
        if kb_id == "default":
            raise ValueError("请创建独立知识库，不导入 default。")
        # Permit interrupted uploads, including a committed upload whose response was lost.
        expected = {d["document_id"] + ".txt": d["content_hash"] for d in manifest["documents"]}
        corpus = await client.get("api/v1/evaluations/corpus", params={"knowledge_base_id": kb_id})
        corpus.raise_for_status()
        for item in corpus.json():
            if expected.get(item["name"]) != item["content_hash"]:
                raise ValueError("目标库包含其他资料，请新建独立知识库。")
        binding_path = folder / ("binding-" + digest(url + ":" + kb_id)[:12] + ".json")
        bindings = {}
        for doc in manifest["documents"]:
            response = await client.post(
                f"api/v1/knowledge-bases/{kb_id}/files",
                files={"file": (doc["document_id"] + ".txt", contents[doc["document_id"]])},
                headers={"Idempotency-Key": doc["document_id"]},
            )
            response.raise_for_status()
            bindings[doc["document_id"]] = response.json()
            # Persist each success so an interrupted import is safely resumable.
            save_json(binding_path, {"url": url, "kb_id": kb_id, "files": bindings})
        all_bound = bound_cases(manifest["cases"], bindings)
        save_json(folder / "cases-bound.json", all_bound)
        versions = []
        for split in ("development", "acceptance"):
            selected = [c for c in manifest["cases"] if c["split"] == split]
            for start in range(0, len(selected), 20):
                name = f"CRUD-{manifest['sampling']['seed']}-{SHA256[:8]}-{split}-{start // 20 + 1}"
                body = CaseSetRequest.model_validate(
                    {
                        "knowledge_base_id": kb_id,
                        "name": name,
                        "split": split,
                        "review_status": "draft",
                        "frozen": False,
                        "tags": ["crud-rag", "review-pending"],
                        "cases": bound_cases(selected[start : start + 20], bindings),
                    }
                )
                response = await client.post("api/v1/evaluations/case-sets", json=body.model_dump())
                response.raise_for_status()
                versions.append(response.json())
                save_json(folder / "case-sets-bound.json", versions)
                save_json(
                    folder / f"cases-{split}-{start // 20 + 1}.json", body.model_dump()["cases"]
                )
        if index:
            response = await client.post(f"api/v1/knowledge-bases/{kb_id}/index")
            response.raise_for_status()
        return {
            "kb_id": kb_id,
            "documents": len(bindings),
            "case_sets": len(versions),
            "index_requested": index,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="下载固定版本的官方原始数据，无模型调用")
    download.add_argument("--output", type=Path, default=CACHE / "upstream.json")
    prepare = commands.add_parser("prepare", help="离线制作子集，无数据库或模型调用")
    prepare.add_argument("--input", type=Path, default=CACHE / "upstream.json")
    prepare.add_argument("--output", type=Path, default=CACHE / "subset-v1")
    prepare.add_argument("--per-task", type=int, default=20)
    prepare.add_argument("--distractors", type=int, default=100)
    prepare.add_argument("--seed", type=int, default=22)
    check = commands.add_parser("validate")
    check.add_argument("--folder", type=Path, default=CACHE / "subset-v1")
    revise = commands.add_parser("revise", help="应用有原因的排除清单，另存版本，不覆盖原数据")
    revise.add_argument("--folder", type=Path, default=CACHE / "subset-v1")
    revise.add_argument("--output", type=Path, default=CACHE / "subset-v1-reviewed")
    revise.add_argument(
        "--reviews", type=Path, default=ROOT / "evals/crud-rag/review-overrides-v1.json"
    )
    upload = commands.add_parser("import", help="导入独立知识库，按最多 20 题分批保存草稿")
    upload.add_argument("--folder", type=Path, default=CACHE / "subset-v1")
    upload.add_argument("--url", default="http://127.0.0.1:8000")
    upload.add_argument("--kb-id", required=True)
    upload.add_argument("--index", action="store_true", help="显式构建索引，可能调用 Embedding")
    args = parser.parse_args()
    if args.command == "download":
        response = httpx.get(
            f"https://api.github.com/repos/IAAR-Shanghai/CRUD_RAG/git/blobs/{BLOB}",
            headers={"Accept": "application/vnd.github.raw+json"},
            timeout=300,
            follow_redirects=True,
        )
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != SHA256:
            raise ValueError("下载内容校验失败，未保存。")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(response.content)
        print(f"已下载并校验：{args.output}")
    elif args.command == "prepare":
        manifest = build_subset(
            read_upstream(args.input),
            per_task=args.per_task,
            distractors=args.distractors,
            seed=args.seed,
        )
        write_subset(manifest, args.output)
        validate_bundle(args.output)
        print(f"已制作并校验：{args.output / 'SUMMARY.md'}")
    elif args.command == "validate":
        manifest, contents = validate_bundle(args.folder)
        print(f"校验通过：{len(manifest['cases'])} 题，{len(contents)} 篇文档。")
    elif args.command == "revise":
        count = revise_bundle(args.folder, args.output, args.reviews)
        print(f"已另存修订版本，排除 {count} 题：{args.output}")
    else:
        from app.config import Settings

        token = os.getenv("CRUD_RAG_API_TOKEN") or Settings().api_token.get_secret_value()
        result = asyncio.run(
            import_bundle(args.folder, args.url, args.kb_id, index=args.index, token=token)
        )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
