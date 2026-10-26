"""导出并审计已保存的测评集；只读，不修改数据库。

用法：
  uv run python -m scripts.export_evaluation_case_sets
  uv run python -m scripts.export_evaluation_case_sets --output evals/case-sets/2026-10-26
"""

import argparse
import asyncio
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.database import Database

DEFAULT_OUTPUT = Path("evals/case-sets") / datetime.now(timezone.utc).date().isoformat()


def case_audit(cases):
    expected = Counter(case.get("expected", "unlabelled") for case in cases)
    scopes = Counter(case.get("judgment_scope", "full") for case in cases)
    references = sum(bool((case.get("reference_answer") or "").strip()) for case in cases)
    return {
        "expected": dict(expected),
        "judgment_scope": dict(scopes),
        "with_evidence": sum(bool(case.get("evidence_groups")) for case in cases),
        "with_negative_files": sum(bool(case.get("negative_file_ids")) for case in cases),
        "with_reference_answer": references,
    }


async def load_sets(database):
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT c.*,k.name AS kb_name,"
            "(SELECT COUNT(*) FROM evaluations e "
            " WHERE e.request->>'case_set_id'=c.case_set_id) AS evaluation_refs "
            "FROM evaluation_case_sets c "
            "JOIN knowledge_bases k ON k.kb_id=c.kb_id "
            "ORDER BY k.name,c.created_at,c.name"
        )
        return await cursor.fetchall()


async def main(output: Path):
    database = Database(Settings())
    async with database.pool:
        rows = await load_sets(database)

    output.mkdir(parents=True, exist_ok=True)
    sets = []
    for row in rows:
        sets.append(
            {
                "case_set_id": row["case_set_id"],
                "knowledge_base": {"kb_id": row["kb_id"], "name": row["kb_name"]},
                "name": row["name"],
                "version": row["version"],
                "tags": row["tags"],
                "split": row["split"],
                "review_status": row["review_status"],
                "frozen": row["frozen"],
                "archived": row["archived"],
                "fingerprint": row["fingerprint"],
                "created_at": row["created_at"],
                "evaluation_refs": row["evaluation_refs"],
                "case_count": len(row["cases"]),
                "corpus_count": len(row["corpus"]),
                "audit": case_audit(row["cases"]),
                "cases": row["cases"],
                "corpus": row["corpus"],
            }
        )

    duplicate_names = defaultdict(list)
    for item in sets:
        duplicate_names[item["name"]].append(item["knowledge_base"]["name"])
    duplicates = {name: kbs for name, kbs in duplicate_names.items() if len(kbs) > 1}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "evaluation_case_sets (read-only export)",
        "counts": {
            "sets": len(sets),
            "knowledge_bases": len({item["knowledge_base"]["kb_id"] for item in sets}),
            "distinct_names": len(duplicate_names),
            "cases": sum(item["case_count"] for item in sets),
        },
        "duplicate_names": duplicates,
        "sets": sets,
    }
    (output / "case-sets.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "知识库",
                "测评集",
                "版本",
                "题数",
                "语料数",
                "拆分",
                "审核状态",
                "冻结",
                "已归档",
                "已引用评测数",
                "答案题",
                "应拒答题",
                "部分标注",
                "含必要证据",
                "含负相关文件",
                "标签",
            ]
        )
        for item in sets:
            audit = item["audit"]
            writer.writerow(
                [
                    item["knowledge_base"]["name"],
                    item["name"],
                    item["version"],
                    item["case_count"],
                    item["corpus_count"],
                    item["split"],
                    item["review_status"],
                    "是" if item["frozen"] else "否",
                    "是" if item["archived"] else "否",
                    item["evaluation_refs"],
                    audit["expected"].get("answerable", 0),
                    audit["expected"].get("unanswerable", 0),
                    audit["judgment_scope"].get("partial", 0),
                    audit["with_evidence"],
                    audit["with_negative_files"],
                    ", ".join(item["tags"]),
                ]
            )

    print(f"output={output}")
    print(
        f"sets={payload['counts']['sets']} kbs={payload['counts']['knowledge_bases']} "
        f"names={payload['counts']['distinct_names']} cases={payload['counts']['cases']}"
    )
    for name, kbs in sorted(duplicates.items()):
        print(f"duplicate_name={name} kbs={len(kbs)}")
    for item in sets:
        audit = item["audit"]
        partial = audit["judgment_scope"].get("partial", 0)
        print(
            f"{item['name']} | {item['knowledge_base']['name']} | "
            f"n={item['case_count']} | {item['split']} | {item['review_status']} | "
            f"archived={item['archived']} | refs={item['evaluation_refs']} | "
            f"partial={partial} | evidence={audit['with_evidence']}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    asyncio.run(main(args.output))
