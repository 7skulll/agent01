# enterprise-langchain-rag

企业知识库 RAG 学习项目 · FastAPI + LangChain v1 + LangGraph + SQLite

当前已完成 **Agent 对话/可恢复任务**、**文件上传** 与 **文件解析切片** 三段；
**尚未实现向量化、向量存储与检索**。

## 技术栈

- Python 3.11–3.13，FastAPI + Uvicorn
- LangChain v1 + LangGraph，`AsyncSqliteSaver` 检查点
- SQLite（`aiosqlite`）持久化会话、任务、文件与切片
- `pydantic-settings` 配置（环境变量前缀 `RAG_`）

## 当前范围

已实现：

- 会话与历史：创建/列出/查询会话，消息落库
- 可恢复任务：显式 LangGraph 图 `control → model → validate → tools`，
  支持暂停/恢复/取消、持久化 SSE 事件与游标重连、幂等键、调用预算
- 文件上传：multipart 单文件（20 MiB 硬限）、扩展名白名单、文件名安全、
  `Idempotency-Key` 幂等、SHA-256 内容哈希、本地对象存储（临时文件 + 硬链接原子发布）
- 文件解析与切片：`.txt/.md/.pdf/.docx/.xlsx` → 结构化 Block → 递归切片 + 重叠，
  切片落库并推进状态机

尚未实现（后续阶段）：

- 向量化与向量存储（embedding）
- 检索：向量召回 + 关键词召回（FTS5）→ RRF 融合 → 可选 Rerank → token 预算裁剪
- 将检索接入对话图、引用回填、评估闭环
- 文件下载/删除、孤儿文件 GC、多知识库

明确不做：OCR、图片处理、表格数值计算。

## 快速开始

```bash
uv sync
cp .env.example .env      # 填入 LLM 配置
uv run uvicorn app.main:app --reload
```

API 文档：<http://127.0.0.1:8000/docs>

### 环境变量（`.env`，前缀 `RAG_`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `RAG_LLM_MODEL` / `RAG_LLM_BASE_URL` / `RAG_LLM_API_KEY` | 模型配置 | 空 |
| `RAG_CHAT_DATABASE_PATH` | SQLite 路径 | `data/chat.sqlite3` |
| `RAG_FILE_STORAGE_PATH` | 本地对象存储根目录 | `data/files` |
| `RAG_CHUNK_SIZE_TOKENS` / `RAG_CHUNK_OVERLAP_TOKENS` | 切片大小与重叠（估算 token） | `512` / `64` |

## 目录结构

```
app/
  main.py            # 应用装配：lifespan 建表，装配各 Service
  config.py          # Settings
  conversations.py   # ConversationRepository（连接 + 事务锁）
  error / scheme / streaming 等
  agent/             # AgentRuntime、prompt、校验
  runs.py / run_graph.py / run_repository.py / run_api.py   # 可恢复任务与 SSE
  rag/
    storage.py       # LocalStorage：原子发布、越界防护
    files.py         # FileService/Repository、文件名安全、上传补偿
    file_api.py      # 上传 / 列表 / 触发索引路由
    parsers.py       # 各格式 -> Block
    chunking.py      # 结构分组 + 递归切片 + 重叠
    indexing.py      # chunks 表 + 状态机 + 批量任务
docs/                # 接口契约与交接文档
tests/               # pytest 测试
```

## API 一览（前缀 `/api/v1`）

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 服务与模型就绪状态 |
| `POST /conversations`、`GET /conversations`、`GET /conversations/{id}` | 会话 |
| `POST /conversations/{id}/runs` | 提交任务（202） |
| `GET /runs/{id}`、`GET /runs/{id}/events` | 查询 / SSE 订阅 |
| `POST /runs/{id}/pause`、`/resume`、`/cancel` | 任务控制 |
| `POST /knowledge-bases/default/files` | 上传（multipart + `Idempotency-Key`） |
| `GET /knowledge-bases/default/files` | 文件列表与解析状态 |
| `POST /knowledge-bases/default/index` | 批量解析切片（202） |

知识库当前仅支持 `default`，其他返回 404。

## 开发

```bash
uv run pytest -q          # 测试
uv run ruff check .       # 静态检查
uv run ruff format --check .
```

> 详细契约见 `docs/backend-api-plan.md`，交接说明见 `docs/handover.md`。
