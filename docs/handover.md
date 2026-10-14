# 交接文档（langchain-rag）

> 更新时间：本次会话结束前。用于下次继续开发时快速接手。
> 关联文档：`docs/backend-api-plan.md`（接口契约）、`docs/run-flow.md`（任务/SSE 契约）。

## 0. 一句话现状

已完成 **Agent 对话/可恢复任务** + **文件上传** + **文件解析切片** 三段；**尚未做 Embedding、向量存储、检索**。
测试与静态检查全绿：`uv run pytest -q` → **100 passed**；`uv run ruff check .` / `ruff format --check .` → 干净。

> ⚠️ **重要**：当前工作区有大量未提交改动（`git status` 显示 `app/rag/`、`docs/backend-api-plan.md`、多个测试文件为 untracked，`app/chat.py`/`tests/test_chat.py` 等为 deleted）。**下次动手前先确认是否需要 `git add/commit`**，避免丢失或误删。

---

## 1. 技术栈与目录

- Python 3.11–3.13，FastAPI + LangChain v1 + LangGraph + `AsyncSqliteSaver`，SQLite（`aiosqlite`）。
- 依赖见 `pyproject.toml`：fastapi、uvicorn、pydantic-settings、langchain、langchain-deepseek、langgraph、langgraph-checkpoint-sqlite、**pypdf、python-docx、openpyxl**。
- 运行：`uv sync` → 复制 `.env.example` 为 `.env` 填模型 → `uv run uvicorn app.main:app --reload`。

```
app/
  main.py            # 应用装配：lifespan 建表、装配 FileService / IndexService / RunService
  config.py          # Settings（env 前缀 RAG_）
  errors.py          # ChatError（安全错误结构）+ upstream_error
  conversations.py   # ConversationRepository：连接、事务锁、会话表
  schemas.py         # 对话/任务 Pydantic 模型
  agent/             # AgentRuntime、prompt、校验（LangChain create_agent）
  runs.py / run_graph.py / run_repository.py / run_api.py   # 可恢复任务与 SSE
  rag/
    storage.py       # LocalStorage：本地对象存储
    files.py         # FileService/FileRepository、文件名安全、上传补偿
    file_api.py      # 上传 / 列表 / 触发索引 路由
    parsers.py       # 各格式 -> Block（文本产出，不做切片）
    chunking.py      # 结构分组 + 递归切片 + 重叠 + token 估算
    indexing.py      # IndexRepository(chunks) + IndexService(状态机/批量任务)
tests/
  conftest.py support.py test_files.py test_index.py test_runs.py test_run_failures.py test_shared.py
```

---

## 2. 已完成工作

### 2.1 Agent 对话与可恢复任务（较早完成）

- `/chat` 之外，新增显式 LangGraph 状态图 `control → model → validate → tools`，支持任务状态、节点边界暂停/恢复/取消、持久化 SSE 事件与游标重连、幂等键、调用预算、错误分类。契约见 `docs/run-flow.md`。
- 会话历史存 SQLite；检查点用 `AsyncSqliteSaver`。

### 2.2 文件上传（`app/rag/storage.py` + `files.py` + `file_api.py`）

- `LocalStorage`：`put/get/delete/exists`；路径规则 `{kb_id}/{file_id}{ext}`；`resolve()` 越界防护；**单遍 SHA-256 + 临时文件 + 硬链接原子发布**（不覆盖已有对象），失败清理临时文件。
- 上传接口 `POST /api/v1/knowledge-bases/{kb_id}/files`：
  - multipart 单文件，**20 MiB 硬限**（接收期即限）、扩展名白名单 `.pdf/.doc/.docx/.md/.txt/.xls/.xlsx`。
  - 文件名安全：UTF-8 百分号解码、latin-1→UTF-8 修复、basename（含 `\`）、NFC、字符白名单、**UTF-8 ≤255 字节**。
  - `Idempotency-Key`：相同键 + 相同（文件名、内容哈希）返回原记录；不一致返回 409。
  - `content_hash` = SHA-256，**只作普通索引，不强制内容去重**（同名可重复上传，各自独立 `file_id`）。
- `files` 表字段：`file_id, kb_id, name, extension, size, content_hash, storage_path, storage_backend, index_status, index_error, created_at, updated_at, idempotency_key`；索引 `(kb_id,name)`、`content_hash`、唯一 `(kb_id,idempotency_key)`、唯一 `storage_path`。
- 测试：`tests/test_files.py`（29 项）。

### 2.3 文件解析与切片（`app/rag/parsers.py` + `chunking.py` + `indexing.py`）

- 解析器（只产出 `Block`，`kind` 为 `text`/`table`，可带 `heading`/`page`）：
  - `.txt`：UTF-8/GB18030 解码 → 段落。
  - `.md`：标题栈 → `heading` 路径（如 `第一章 > 1.1 小节`）。
  - `.pdf`：pypdf 逐页文本；**无文本层（扫描件）明确失败**。
  - `.docx`：`iter_inner_content()` 段落 + 表格；标题样式生成 `heading`；**表头拼进每行**。
  - `.xlsx`：openpyxl，每个 sheet 一行一条文本，表头拼进每行。
  - `.doc`/`.xls`：明确失败（提示转换）。
- 切片：先按 `(heading, kind, page)` 分组，再按分隔符递归切分（段落→换行→中英文句末→分号→逗号→空格→硬切），最后按 token 合并并加重叠。
- token 为**确定性估算**（CJK 记 1，其余 4 字符记 1），不依赖外部 tokenizer。
- 配置：`RAG_CHUNK_SIZE_TOKENS=512`、`RAG_CHUNK_OVERLAP_TOKENS=64`。
- `chunks` 表：`chunk_id, kb_id, file_id, chunk_index, content, tokens, heading, kind, page, created_at`；唯一 `(file_id, chunk_index)`。
- 批量接口 `POST /api/v1/knowledge-bases/{kb_id}/index`：**202**，调度该库下所有 `not_indexed`/`failed` 文件；单文件失败不影响其他文件；同一库运行中返回 **409 `index_in_progress`**。
- 列表接口 `GET /api/v1/knowledge-bases/{kb_id}/files`：分页，返回 `index_status/index_error/chunk_count`。
- 状态机（**复用 `recorded`，未改表结构**）：`not_indexed → parsing → recorded`；失败 `failed` 写入 `index_error`；启动时把遗留 `parsing` 重置为 `not_indexed`。
- 测试：`tests/test_index.py`（22 项）。

### 2.4 已修复的 4 个 P1 问题（本轮）

1. **上传补偿误删已提交文件**：删除对象前先探测记录是否落库，已提交则返回记录、不删；探测失败保守不删。
2. **并发索引重复 worker**：`schedule` 用 `asyncio.Lock` 串行化「检查→查询待处理→注册任务」。
3. **切片写库失败卡 `parsing`**：切片写入与状态推进在**同一事务**提交；失败整体回滚并尽力置 `failed`；`_run` 单文件异常不中断整批。
4. **解析阻塞事件循环**：`parse`/`chunk_blocks` 走 `asyncio.to_thread`；`_hard_split` 由 O(n²) 改 O(n)；`_merge` 保证块不超上限。

---

## 3. 数据模型速查

- `conversations`：会话元数据 + 成功检查点指针。
- `runs` / `run_events`：任务与 SSE 事件。
- `files`：见 2.2。
- `chunks`：见 2.3。
- `index_status` 取值：`not_indexed / recorded / parsing / indexing / indexed / failed`。
  - **当前语义**：`recorded` = 已解析、切片已入库、**待向量化**；`indexing`/`indexed` 尚未使用（留给下一阶段）。

## 4. API 一览（前缀 `/api/v1`）

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

知识库当前只支持 `default`，其他返回 404。

---

## 5. 下一步工作（建议顺序）

1. **向量化阶段**：读 `recorded` 文件 → 调 Qwen Embedding（批量/限流/重试）→ 写向量存储 → 状态 `recorded → indexing → indexed`；记录所用 `embedding_model` 与维度，换模型需重建。
2. **检索阶段**：向量召回 + 关键词召回（建议 SQLite FTS5）→ **RRF 融合**（借鉴 FastGPT）→ 可选 Rerank（借鉴 RAGFlow：喂自然文本、分数归一化、候选池大于 top-k）→ 去重 → **token 预算裁剪**（至少保留一条）。
3. **接入对话**：在 LangGraph 图里加检索节点/工具；追问改写（多查询）；资料不足时明确拒答。
4. **引用**：答案句与 chunk 相似度回填引用编号（参考 RAGFlow `insert_citations`）。
5. **评估闭环**：固定问题集，对比切片参数 / 混合召回 / Rerank，记录命中率、正确率、拒答质量、延迟、费用。
6. **健壮性/产品化**：文件下载/删除接口、孤儿文件 GC、多知识库、文件数量上限。

---

## 6. 需要注意的问题

**范围与边界**
- 明确不做：OCR、图片、表格数值计算、检索、引用（截至目前）。
- 上传成功 **不等于** 可检索；`recorded` 只是「切片就绪」。前端不要把 `recorded` 显示成「可检索」。

**一致性 / 并发**
- 存储与 SQLite 非同一事务：上传走「先探测后删」补偿；**崩溃留下的孤儿文件尚无 GC**（本地端目前只保证不留半截文件，`.part` 会被清理）。
- 单进程单 worker。索引批量任务注册在内存（`IndexService.tasks`），重启后靠 `reconcile()` 把 `parsing` 重置；若持久化失败且 `failed` 标记也失败，文件会停在 `parsing`，**只能重启恢复**。
- `pending()` 只选 `not_indexed`/`failed`，`parsing` 不重复处理（避免打断正在处理的文件）。

**解析 / 切片**
- token 是估算值，不是模型真实 tokenizer；做上下文预算时若需精确，可换 tiktoken（注意离线不可用）。
- 强约束：**空文件、无文本层、损坏文件、旧格式都必须显式失败**，不得产出空切片。
- 大文件解析已移出事件循环，但整体仍是 CPU 密集，后续可考虑限并发。

**代码层面**
- `storage.py` 里仍有一个 `Storage(Protocol)`，仅在 `files.py` 作类型标注；若坚持「只本地」可删（两行改动），当前保留无副作用。
- 测试里 PDF 文本用例使用**手工构造的最小 PDF**（`tests/test_index.py`），不是真实样本；真实 PDF 兼容性需另测。
- 环境变量在 `.env`（已被忽略提交），勿提交密钥。

**Git**
- 当前大量改动**未提交**。交接/切换前建议先 review 并提交，或至少备份。
- `README.md`、`docs/rag-flow.md`、`app/chat.py`、`tests/test_chat.py` 等已被删除/替换，注意别误恢复。

---

## 7. 下次开工检查清单

1. `cd RAG/langchain-rag && uv sync`
2. `uv run pytest -q` 应 **100 passed**；`uv run ruff check .` 干净。
3. 确认 `.env` 模型配置（`RAG_LLM_MODEL/BASE_URL/API_KEY`）。
4. 确认是否已提交当前改动（见第 0 节警告）。
5. 从「第 5 节 第 1 步：向量化阶段」开始。
