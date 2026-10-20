"""Prepare 30 source-backed project-document cases; import only with explicit CLI flags."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
# Scene groups are kept in a single split to avoid near-duplicate train/test questions.
SCENES = [
    (
        "management",
        "docs/daily-management.md",
        "development",
        [
            ("下载文件会在浏览器中直接解释吗？", "禁止浏览器内联解释"),
            ("删除资料后历史答案和来源还在吗？", "历史回答及其来源快照保留"),
            ("上传队列会并行上传文件吗？", "上传队列串行执行"),
            ("上传残留清理会删除刚写入的文件吗？", "超过 24 小时的普通文件"),
            ("上传残留会在什么时候自动清理？", "服务启动时也执行一次"),
            ("问答文件范围为空会回退全库吗？", "范围为空，不会回退全库"),
        ],
    ),
    (
        "answer_protocol",
        "docs/structured-answer-v2.md",
        "development",
        [
            ("知识库回答会展示模型原始草稿吗？", "不展示模型原始 JSON 或修复前草稿"),
            ("一个回答段落可以引用多少个来源？", "source_ids 每段 1–20 项"),
            ("回答格式不合法最多修复几次？", "最多一次修复模型调用"),
            ("修复回答时会重新检索资料吗？", "不重查资料、不修改来源集合"),
            ("回答能引用已经裁掉的候选吗？", "不能引用已裁掉的候选或不存在的编号"),
            ("协议错误会静默改成资料不足吗？", "不能自动降级为资料不足"),
        ],
    ),
    (
        "context",
        "docs/context-assembly-v1.md",
        "development",
        [
            ("邻居补全最多使用多少资料预算？", "邻居最多消耗资料预算的 20%"),
            ("重叠去重会模糊删除数字或缩进吗？", "不模糊删除数字、标点或缩进"),
            ("表格和代码会为了预算被随意切断吗？", "表格、列表和代码保持完整块"),
            (
                "资料与问题通过什么格式进入最终消息？",
                "通过 JSON 序列化承载 evidence 和原始 question",
            ),
            ("失败恢复会使用最新资料重新组装吗？", "不按最新知识库内容重新组装"),
            ("完整请求超预算时先丢历史还是资料？", "必要时先去除最旧历史、再收缩资料"),
        ],
    ),
    (
        "evaluation",
        "docs/evaluation-v3.md",
        "acceptance",
        [
            ("部分标注题能进入完整检索质量均值吗？", "完整正相关案例才参与五项质量均值"),
            ("检索精准率的分母是实际返回数量吗？", "文件去重、固定 K 分母、完整标注失败题计零"),
            ("部分标注可以证明资料里不存在答案吗？", "部分标注不能证明资料中不存在答案"),
            ("某一组必要证据无法映射时会给覆盖率零分吗？", "整题证据覆盖不评分"),
            ("文件命中率等于回答事实正确率吗？", "文件级命中分开"),
            ("评测原文包含检查是语义事实验证吗？", "不是语义事实验证"),
        ],
    ),
    (
        "settings",
        "docs/model-settings.md",
        "acceptance",
        [
            ("模型设置保存时会请求远端模型吗？", "保存不发送外部请求，也不自动重建索引"),
            ("API Key 留空会清空密钥吗？", "API Key 留空或省略时保留该模型现有密钥"),
            ("恢复一个模型的环境配置会影响另一个模型吗？", "另一类模型保持不变"),
            ("模型配置覆盖保存在什么位置？", "配置覆盖保存在 PostgreSQL 的 model_settings 表中"),
            ("模型配置接口会返回 API Key 吗？", "不回传密钥"),
            (
                "只轮换 API Key 会改变向量索引版本吗？",
                "只轮换 API Key 且其他字段不变不会改变向量索引版本",
            ),
        ],
    ),
]


def build_manifest():
    sources, cases = [], []
    for scene, relative, split, questions in SCENES:
        raw = (ROOT / relative).read_bytes()
        text = raw.decode()
        content_hash = hashlib.sha256(raw).hexdigest()
        sources.append({"path": relative, "content_hash": content_hash})
        for query, quote in questions:
            if quote not in text:
                raise ValueError(f"原文已变化，需人工核对案例：{relative}: {query}")
            cases.append(
                {
                    "query": query,
                    "source": relative,
                    "quote": quote,
                    "split": split,
                    "judgment_scope": "partial",
                    "expected": "answerable",
                    "tags": ["project-docs", "review-pending", scene],
                }
            )
    return {
        "name": "项目文档案例草稿",
        "review_status": "draft",
        "frozen": False,
        "notice": "30 道真实项目文档出处的待核验案例，不是用户业务标注或生产签收标准。",
        "sources": sources,
        "cases": cases,
    }


async def import_manifest(manifest, url, kb_id, *, index=False):
    async with httpx.AsyncClient(base_url=url.rstrip("/") + "/", timeout=60) as client:
        files = {}
        for source in manifest["sources"]:
            path = ROOT / source["path"]
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != source["content_hash"]:
                raise ValueError("导入前资料已变化，请重新生成案例草稿。")
            response = await client.post(
                f"api/v1/knowledge-bases/{kb_id}/files",
                files={"file": (path.name, raw)},
                headers={"Idempotency-Key": "project-v3-" + source["content_hash"]},
            )
            response.raise_for_status()
            files[source["path"]] = response.json()
        versions = []
        for split in ("development", "acceptance"):
            cases = []
            for draft in manifest["cases"]:
                if draft["split"] != split:
                    continue
                file = files[draft["source"]]
                cases.append(
                    {
                        "query": draft["query"],
                        "expected": "answerable",
                        "judgment_scope": "partial",
                        "relevant_file_ids": [file["file_id"]],
                        "tags": draft["tags"],
                        "required_facts": "待人工核验：" + draft["quote"],
                        "evidence_groups": [
                            {
                                "fact": draft["query"],
                                "alternatives": [
                                    {
                                        "file_id": file["file_id"],
                                        "content_hash": file["content_hash"],
                                        "quote": draft["quote"],
                                    }
                                ],
                            }
                        ],
                    }
                )
            response = await client.post(
                "api/v1/evaluations/case-sets",
                json={
                    "knowledge_base_id": kb_id,
                    "name": "项目文档待核验-" + split,
                    "split": split,
                    "review_status": "draft",
                    "frozen": False,
                    "tags": ["project-docs", "review-pending"],
                    "cases": cases,
                },
            )
            response.raise_for_status()
            versions.append(
                {key: response.json()[key] for key in ("case_set_id", "name", "version", "split")}
            )
        if index:
            response = await client.post(f"api/v1/knowledge-bases/{kb_id}/index")
            response.raise_for_status()
        return versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/cases/project-v3-draft.json")
    parser.add_argument("--import-url", help="显式指定本地服务地址后上传资料并保存两个草稿集合")
    parser.add_argument("--kb-id", help="建议先在界面新建专用知识库，填写其 ID")
    parser.add_argument("--index", action="store_true", help="导入后启动索引，可能调用 Embedding")
    args = parser.parse_args()
    if (args.import_url and not args.kb_id) or (args.index and not args.import_url):
        parser.error("导入需要 --import-url 与 --kb-id；--index 仅能与导入一起使用。")
    manifest = build_manifest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"已生成 {len(manifest['cases'])} 道待核验案例：{args.output}")
    if args.import_url:
        print(
            json.dumps(
                asyncio.run(
                    import_manifest(manifest, args.import_url, args.kb_id, index=args.index)
                ),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
