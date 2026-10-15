"""真实 Embedding 与索引检索冒烟测试，使用临时数据库和合成资料。"""

import asyncio
import tempfile
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app
from app.rag.embedding import build_embedding_model


async def main():
    settings = Settings()
    model = build_embedding_model(settings)
    if model is None:
        raise SystemExit("Embedding 配置不完整。")
    vectors = await model.embed_documents(["用于验证向量接口的测试文本。", "第二条测试文本。"])
    query = await model.embed_query("测试文本")
    print(f"Embedding 调用成功：文档数量={len(vectors)}，维度={len(query)}")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = settings.model_copy(
            update={
                "chat_database_path": root / "chat.sqlite3",
                "file_storage_path": root / "files",
            }
        )
        app = create_app(settings, embedding_model=model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                prefix = "/api/v1/knowledge-bases/default"
                response = await client.post(
                    prefix + "/files",
                    files={"file": ("测试制度.txt", "公司员工每年享有十天带薪年假。".encode())},
                    headers={"Idempotency-Key": "smoke-file"},
                )
                response.raise_for_status()
                (await client.post(prefix + "/index")).raise_for_status()
                await app.state.index_service.wait()
                files = (await client.get(prefix + "/files")).json()["items"]
                if files[0]["index_status"] != "indexed":
                    raise SystemExit("真实向量化未完成，请检查文件状态。")
                response = await client.post(
                    prefix + "/search", json={"query": "员工有多少天年假？"}
                )
                response.raise_for_status()
                sources = response.json()["sources"]
                if not sources or "十天" not in sources[0]["content"]:
                    raise SystemExit("真实检索未命中测试资料。")
                print("上传 → 解析 → indexed → 混合检索：通过（临时数据已隔离）")


if __name__ == "__main__":
    asyncio.run(main())
