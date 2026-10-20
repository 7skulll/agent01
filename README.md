# enterprise-langchain-rag

企业知识库 RAG 项目 · FastAPI + LangChain v1 + LangGraph + PostgreSQL/pgvector

当前已完成 **Agent 可恢复任务、文件解析切片、Embedding 向量化、pgvector 精确向量检索、
PostgreSQL BM25 混合检索与带引用的知识库问答**。

文档导航与待办见 [docs/README.md](docs/README.md)。

## 技术栈

- Python 3.11–3.13，FastAPI + Uvicorn
- LangChain v1 + LangGraph，`AsyncPostgresSaver` 检查点
- PostgreSQL（`psycopg` 异步连接池）持久化业务数据与检查点，pgvector 存储向量
- `pydantic-settings` 配置（环境变量前缀 `RAG_`）

## 当前范围

已实现：

- 会话与历史：创建/列出/查询会话，消息落库
- 可恢复任务：显式 LangGraph 图 `control → model → validate → tools`，
  支持暂停/恢复/取消、持久化 SSE 事件与游标重连、幂等键、调用预算
- 文件上传：multipart 单文件（50 MiB 硬限）、扩展名白名单、文件名安全、
  `Idempotency-Key` 幂等、SHA-256 内容哈希、本地对象存储（临时文件 + 硬链接原子发布）
- 文件解析与切片：`.txt/.md/.pdf/.docx/.xlsx` → 结构化 Block → 递归切片 + 重叠，
  切片落库并推进状态机
- 向量化：批处理、有限重试、数量/维度/有限值校验，文件向量与 indexed 状态原子提交
- 检索：pgvector 精确余弦召回 + PostgreSQL 位置倒排 BM25 → RRF 排名融合 → 内容去重
- 知识库问答：显式检索节点、上下文预算、结构化引用、资料不足、来源持久化与 SSE 输出
- 日常管理：单文件下载/删除、上传残留清理、会话改名/删除、问答文件范围
- 模型设置：前端配置自有 LLM / Embedding API Key、地址与模型，服务端持久化并即时生效
- 同页评测：案例集核验/冻结、独立实验参数、检索/回答质量总览、标签筛选、分页归档与失败题重试

尚未实现（后续阶段）：

- Rerank 业务评估集与相关性阈值校准
- 大规模 ANN 的真实业务召回率/负载调优（已有可选 HNSW 接入）
- 业务问答事实支持、拒答与上下文效果的固定问题集评估

明确不做：OCR、图片处理、表格数值计算。

## 快速开始

先按下文“本机 PostgreSQL 配置与验证”准备数据库。当前开发环境使用 Homebrew PostgreSQL 16
和 pgvector 0.8.2，数据库用户、业务库和测试库已创建。

```bash
uv sync
cp .env.example .env      # 首次配置，填写 LLM、Embedding 及其单条 token 上限；已有 .env 时保留原配置
brew services start postgresql@16
uv run uvicorn app.main:app --reload
```

API 文档：<http://127.0.0.1:8000/docs>

### 前端工作台

第一版 Vue 3 + TypeScript 工作台位于 `frontend/`，包含知识库问答、文件管理与检索调试。
后端运行后，在另一个终端进入 `frontend/` 执行：

```bash
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173>。开发服务器将 `/api` 代理到本机 8000 端口。
问答历史通过新增的 `GET /api/v1/conversations/{id}/turns` 获取，按任务关联最终答案与引用。
文件管理行支持下载和删除；会话列表支持改名和删除；输入框上方可选择问答文件范围。
上传队列会继续处理上传期间重新加入的重试项；会话和问答历史刷新时更新所有已加载页面。
操作说明、删除保护与清理规则见 [日常管理](docs/daily-management.md)。
详细启动、构建和部署说明见 [前端 README](frontend/README.md)，
验证入口、历史证据与待修复问题见 [文档导航](docs/README.md)。

检索调试页新增「RAG 评测」标签：单题或最多 20 题串行执行，支持仅检索/完整 RAG、
文件相关性标注、Precision/Recall/Hit/MRR/nDCG 指标与 K 曲线、证据流转、人工评价及结果导出。
未标注题不计算检索质量，文件级命中不等于回答正确。操作及口径见
[现行第三版](docs/evaluation-v3.md) 支持独立重排/上下文预算实验、质量总览、分页归档、成功题复用重试与验收报告。旧版评测使用说明与规划已合并，历史实测 JSON 保留。

### 自定义模型

侧栏“模型设置”可分别保存对话模型和 Embedding 的服务地址、模型 ID 与 API Key；
支持 OpenAI 兼容对话接口和 DeepSeek，以及 OpenAI 兼容 Embeddings API。密钥留空保留，
也可恢复环境配置。切换 Embedding 后需到知识库重新构建索引。
配置为工作空间共用，持久化在服务端数据库；详见 [模型设置](docs/model-settings.md)。

### 环境变量（`.env`，前缀 `RAG_`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `RAG_LLM_PROVIDER` | `deepseek` 或 `openai`（OpenAI 兼容） | `deepseek` |
| `RAG_LLM_MODEL` / `RAG_LLM_BASE_URL` / `RAG_LLM_API_KEY` | 模型配置 | 空 |
| `RAG_EMBEDDING_MODEL` / `RAG_EMBEDDING_BASE_URL` / `RAG_EMBEDDING_API_KEY` | Embedding 配置 | 空 |
| `RAG_EMBEDDING_DIM` | 期望维度，0 为首次调用推断 | `0` |
| `RAG_EMBEDDING_BATCH_SIZE` / `RAG_EMBEDDING_MAX_RETRIES` | 批大小、最大重试次数 | `10` / `3` |
| `RAG_CONTEXT_TOKENS` / `RAG_INPUT_TOKENS` | 资料预算、总输入及输出预留预算（估算 token） | `2048` / `8192` |
| `RAG_DATABASE_URL` | PostgreSQL 连接字符串 | 见 `.env.example` |
| `RAG_DATABASE_POOL_SIZE` | 业务连接池上限 | `10` |
| `RAG_FILE_STORAGE_PATH` | 本地对象存储根目录 | `data/files` |
| `RAG_CHUNK_SIZE_TOKENS` / `RAG_CHUNK_OVERLAP_TOKENS` | 切片大小与重叠（配置的 tokenizer 单位） | `512` / `64` |

## 目录结构

```
app/
  main.py            # 应用装配：lifespan 建表，装配各 Service
  config.py          # Settings
  database.py        # PostgreSQL 连接池、事务作用域与单 worker 所有权
  migrations/        # 版本化 SQL 迁移
  conversations.py   # ConversationRepository
  error / scheme / streaming 等
  agent/             # AgentRuntime、prompt、校验
  runs.py / run_graph.py / run_repository.py / run_api.py   # 可恢复任务与 SSE
  rag/
    storage.py       # LocalStorage：原子发布、越界防护
    files.py         # FileService/Repository、文件名安全、上传补偿
    file_api.py      # 上传 / 列表 / 触发索引路由
    parsers.py       # 各格式 -> Block
    chunking.py      # 结构分组 + 递归切片 + 重叠
    indexing.py      # chunks 表 + 解析/向量化批量编排
    embedding.py     # OpenAI 兼容 Embedding 客户端
    vectors.py       # pgvector 向量、索引版本与原子发布
    retrieval.py     # 独立搜索接口、RRF 与来源预算
    terms.py         # 索引/查询共用词项规则
tests/               # pytest 测试
```

## API 一览（前缀 `/api/v1`）

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 服务与模型就绪状态 |
| `POST /conversations`、`GET /conversations`、`GET /conversations/{id}` | 会话 |
| `PATCH /conversations/{id}`、`DELETE /conversations/{id}` | 改名 / 删除会话及历史 |
| `GET /knowledge-bases/{kb_id}/files/{file_id}/download`、`DELETE /knowledge-bases/{kb_id}/files/{file_id}` | 下载 / 删除单文件 |
| `POST /maintenance/files/gc` | 清理超过 24 小时的无引用上传残留，重试原文件清理队列 |
| `POST /conversations/{id}/runs` | 提交任务（202） |
| `GET /runs/{id}`、`GET /runs/{id}/events` | 查询 / SSE 订阅 |
| `POST /runs/{id}/pause`、`/resume`、`/cancel` | 任务控制 |
| `POST /knowledge-bases/default/files` | 上传（multipart + `Idempotency-Key`） |
| `GET /knowledge-bases/default/files` | 文件列表与解析状态 |
| `POST /knowledge-bases/default/index` | 批量解析与向量化、重试及模型变更重建（202） |
| `POST /knowledge-bases/default/search` | 独立向量或混合检索 |

知识库支持创建、切换和删除，现有文件自动保留在 `default`（默认知识库）。
新增 `GET /knowledge-bases`、`POST /knowledge-bases`（`{"name":"项目资料"}`）及
`DELETE /knowledge-bases/{kb_id}`。上述文件/索引/检索接口中的 `default` 均可替换为实际 `kb_id`，
问答提交的 `knowledge_base_id` 同样使用所选库 ID；不存在或已删除的知识库返回 404。

前端右上角提供知识库选择器、“+”新建和删除按钮。每个知识库独立管理文件和检索，
刷新会记住所选库；上传队列固定入队时的知识库，不随切换漂移。
删除需在界面确认，会级联删除该库文件记录、切片、向量和全文索引，同时清理本地原文件。
未结束的问答（含暂停/等待恢复）或正在建立索引时返回 409，请先完成或取消任务。
历史问答及来源快照保留；失败任务在知识库删除后不能恢复。可以删除最后一个库，再新建。
原文件清理失败会保留在持久化队列，下次删库或服务启动时重试。
迁移 `005_knowledge_bases.sql` 启动时自动执行，无需重新上传或重建现有索引。

HTTP 业务错误、存储异常和参数校验统一返回 `{"detail":{"code":"...",
"message":"...","retryable":false,"request_id":"..."}}`。数据库连接失败及连接池
超时返回 503 / storage_unavailable，不包含 SQL 或连接凭据；422 校验详情位于
`detail.errors`，只包含 loc / type / msg，不回显原始输入。错误体 request_id 与
当前响应头 X-Request-ID 一致。已经开始发送的 SSE 仍通过 stream_error 表示读取故障。

## 开发

```bash
uv run pytest -q          # 测试
uv run ruff check .       # 静态检查
uv run ruff format --check .
```

## 索引与问答使用

知识库问答在创建任务时支持 `retrieval_mode`（默认 hybrid）：

```json
{"message":"员工年假有多少天？","idempotency_key":"leave-001","knowledge_base_id":"default","retrieval_mode":"keyword"}
```

keyword 仅走全文召回，不要求 Embedding，仍需配置回答模型。hybrid 的启用路径中如有
关键词召回，可在向量不可用且关键词命中时降级；vector 和权重为 1 的 hybrid 要求
Embedding。无匹配资料的 keyword 问答正常返回资料不足，不调用回答模型。
模式会保存在数据库、任务响应及 checkpoint 中；同一幂等键更改模式返回 409。
004_run_retrieval_mode.sql 在启动时自动迁移，旧任务的默认模式为 hybrid，旧 checkpoint
未包含该字段时也沿用 hybrid，无需重新建立文档索引。

上传文件后调用 `POST /api/v1/knowledge-bases/default/index`，轮询文件列表。
配置 Embedding 后状态流为 `not_indexed → parsing → recorded → indexing → indexed`；
未配置时停在 recorded。解析失败为 failed；向量化失败回到 recorded 并写入 index_error，
再次调用 index 会复用原 chunks。重启回收 indexing 状态，不自动执行外部模型调用。

chunk_vectors 保存 chunk_id、文件 ID、模型、维度、索引版本、实际输入 SHA-256 与向量。
索引版本包含模型、服务地址、配置维度、归一化设置和标题拼装版本；配置变化后调用 index
会重建向量，保持 chunk_id。向量检索只读取 indexed 且属于当前向量版本的资料。
原始正文单独保存，Embedding 输入为文件名、标题路径和正文。

独立检索请求：

```http
POST /api/v1/knowledge-bases/default/search
Content-Type: application/json

{"query":"员工年假有多少天？","mode":"hybrid","top_k":5,"min_score":0.2}
```

mode 支持 vector/hybrid/keyword。两路召回独立并行，按各自就绪状态、版本和
file_ids 过滤后才截取候选。关键词索引在切片提交后独立建立，不依赖 chunk_vectors；
未配置 Embedding、向量化进行中或失败时，已建立全文索引的文件仍可用 keyword 检索。
文件列表的 keyword_ready / keyword_error 独立表达全文状态，index_status 继续表达解析与向量阶段。混合模式可在一路失败时降级，diagnostics 标记原因；降级后仍无候选时
报告检索不可用，不把服务故障解释成“没有资料”。

可添加最多两个扩展 queries 和可选 file_ids；空 file_ids 表示不搜索任何文件。例如：

```json
{"query":"年假审批","queries":["带薪年假申请流程"],"file_ids":["文件ID"],"mode":"hybrid"}
```

按 FastGPT 模式分配候选：混合模式每问题向量 80、全文 60；纯向量或纯全文模式各 100。
多查询先在向量/全文路内分别做等权 RRF 并截回该路预算，再做两路加权 RRF（k=60，
默认各 0.5）。空路、零权重路不参与；仅剩一路时保留其顺序和分数，不额外套 RRF。
min_score 仅在纯向量模式对最终候选生效，混合与全文模式不按它过滤。
去重分别比较类型、标题和正文，统一换行符，非代码正文忽略首尾空白；保留标点、
内部空白和代码缩进，避免金额、编号和运算符被错误合并，并保留 alternatives 来源。
表格的空表头及超出表头宽度的列使用“第 N 列”保留非空值。本次解析版本已更新，
已有文件需显式调用 index 重解析并重建向量与全文索引；去重规则对后续搜索直接生效。
每文件上限默认 20，与接口 top_k 上限一致。向量在 PostgreSQL 内精确计算余弦，只返回候选分块及分数，
应用不再读取全部向量或执行 Python 全量扫描。

问答也可通过 `file_ids` 限制当前召回范围；省略/null 为全库，`[]` 为空范围。
范围持久化并参与幂等校验，切换范围需要新的幂等键。

sources 保留 channels、matched_queries、vector_score、keyword_score、rerank_score，
各分数分别表示其原始度量；未从对应通道命中时为 null，不充当可信概率。
diagnostics 包含候选数量、扫描数量、各阶段耗时、降级原因和是否应用重排。

设置 `RAG_RERANK_ENABLED=true` 并配置 `RAG_RERANK_PROVIDER=siliconflow`、
`RAG_RERANK_BASE_URL`（如 `https://api.siliconflow.cn/v1`）、`RAG_RERANK_MODEL` 和
`RAG_RERANK_API_KEY` 后，应用自动装配真实 HTTP Reranker。启用但配置不完整时启动失败；
默认关闭，显式 `create_app(reranker=adapter)` 注入优先。适配器实现异步
`rerank(query, documents) -> list[float]`，分数须与输入文档一一对应，且全部为有限值。
输入为文件名、标题和自然正文。默认重排前 40 个候选，权重为 1 时直接按模型分数排序，
同分保留原召回顺序；显式设置 `RAG_RETRIEVAL_RERANK_WEIGHT` 为 0~1 之间的值时，
按原排名与重排名次做加权 RRF。权重 0 且未设置阈值时跳过模型调用。
10 秒总超时、数量错误或无效分数均回退原排序，失败处理不会修改原候选。
HTTP 适配器按服务返回的 index 恢复输入顺序，拒绝缺失、重复、越界索引及无效分数。
每批连接错误、429 和 5xx 在整次总超时预算内最多重试一次，其他 HTTP 错误不重试。
Retry-After 支持秒数和 HTTP 日期。应用复用并关闭自建 HTTP 连接，注入适配器由调用方管理。

借鉴 FastGPT 的长文本分窗与最高分聚合，配置 `RAG_RERANK_MAX_INPUT_TOKENS` 后，
适配器为查询及模板留预算，将完整正文切为重叠窗口，每个窗口最终复检。
`RAG_RERANK_TOKENIZER` 指定本地计数器；BPE 对其他模型仍是估算，需按真实服务校准。
例如当前 BGE 配置可从 8192 的预估上限开始，结合 `RAG_TOKEN_SAFETY_RATIO` 保留余量。
该值为 0 时交由服务端处理窗口。窗口每批默认 64 条，总窗口最多 1024，超限明确降级，
不静默截断；全部批次完成并校验后，按每个原候选的最高窗口分数返回。

纯向量 min_score 在重排成功后仍生效。可选 `RAG_RETRIEVAL_RERANK_MIN_SCORE` 只过滤
模型原始分数，默认不设置；开启后未重排尾部不能补位，全部低分可返回空资料。
阈值须用业务样本校准，不把分数或返回非空解释为答案有依据，不做批次 min-max 归一化。
诊断提供 rerank_model、rerank_strategy、rerank_threshold、过滤数量、失败原因及耗时。

```bash
uv run python -m scripts.smoke_rerank           # 两条合成资料，少量真实 API 调用，无数据库依赖
uv run python -m scripts.smoke_rerank --enable  # 仅本次验证临时启用，保留 .env 中的开关
uv run python -m scripts.smoke_rerank --window-test  # 真实服务验证长文尾部命中与窗口聚合
uv run python -m scripts.audit_rerank --enable  # 全量 36 题，复用真实分数比较策略
```

重排审计默认覆盖两套合成集的 32 道有答案题和 4 道无答案题，按数据集和题型分别报告。
方法、复现命令与边界见 [重排审计第二版](docs/rerank-audit.md)。

邻居扩展默认关闭；显式配置后，选中正文可回查相邻切片，只允许同文件、同标题、同页、当前版本的正文，
补充片段保留 supporting_chunks。所有来源、补充片段和引用装饰计入上下文预算。
当前问答由 ContextAssembler 统一组装：超长正文按问题抽取原文窗口并标注 truncated；
没有可容纳窗口时跳过该块，全部无法容纳时返回 no_context_fit。表格、列表和代码块整体保留或跳过。

提交知识库问答：

```http
POST /api/v1/conversations/{conversation_id}/runs
Content-Type: application/json

{"message":"员工年假有多少天？","idempotency_key":"leave-001","knowledge_base_id":"default"}
```

不指定 knowledge_base_id 时保持普通 Agent 对话。知识库问答在模型调用前检索，
基础版默认直接使用原问题；显式开启追问补全后，从同库历史窗口中最多取最近 6 条、1000 个估算 token 的消息。
原问题始终保留，JSON 无效或 10 秒超时则回退。可配置 RAG_QUERY_EXPAND=true
允许额外一条表达，连同原问题总共最多 3 个查询。规划调用消耗持久化模型调用预算。
查询计划和资料分别保存到检查点，恢复失败的模型节点时复用两者。空资料返回不足提示。
问答默认取最多 10 条候选，再按总输入及 RAG_CONTEXT_TOKENS 的资料预算组装。
模型返回 rag-answer-v2 JSON（status、segments、reason），每段包含 text 和 source_ids；
服务端校验后生成 `[1]` 等引用标记，格式错误或非法来源最多修复一次。
资料不足属于正常完成的 insufficient_evidence；协议错误与服务故障不会自动转换为资料不足。
GET runs 和 SSE done 事件包含实际被引用的 sources；SSE 另有 query_plan、retrieval_start/retrieval_end，后者包含检索诊断。
引用校验保证编号对应本轮资料，不能证明答案每个事实都受资料支持。

## 验证与边界

```bash
uv run python -m scripts.smoke_embedding  # 少量真实 API 调用，合成资料和临时数据库
```

health 的 embedding_ready 表示客户端配置已装配，不表示远端鉴权或网络验证通过。
运行保持单进程单 worker；同一数据库/schema 的第二实例会被 advisory lock 拒绝，
避免将仍在运行的任务误标为 recovering。连接池支持单进程中的并发数据库请求。
上下文 token 是估算值，应根据模型窗口设置预算；超长结构化行会明确失败。
当前已接入可选真实 Rerank 服务及同页评测/人工审核，尚未建立真实业务质量结论；追问改写为可选能力，默认关闭。
任务图版本为 chat-rag-v6，旧版本未完成任务不支持直接恢复。

当前链路为原问题（可选扩展）→ 全文/精确余弦并行召回 → 路内多查询 RRF → 两路加权 RRF → 去重与 top_k。
查询改写、查询扩展、邻居补全默认关闭，未启用或注入 Reranker 时不做精排。已有可选能力保留，
显式开启后属于扩展链路，不用于本次基础版评估。BM25 索引保留词项的频率和顺序，
查询侧才去重；全文版本独立于向量版本，需显式调用 index 本地重建过期全文索引。

主要配置：RAG_RETRIEVAL_DENSE_WEIGHT=0.5、RAG_RETRIEVAL_ANSWER_CANDIDATES=10、
RAG_RETRIEVAL_MAX_PER_FILE=20、RAG_RETRIEVAL_NEIGHBOR_CHUNKS=0。
配置全表见 .env.example。


## 离线 tokenizer 与模型输入限制

借鉴 RAGFlow 的离线编码表、FastGPT 的消息/工具计数与最终格式化文本校验。
`app/tokenization.py` 提供独立计数器，Embedding 与聊天可分别设置
`RAG_EMBEDDING_TOKENIZER` / `RAG_CHAT_TOKENIZER`：支持 `cl100k_base`（默认）、
`o200k_base` 和显式旧估算模式 `heuristic`。两张官方编码表随包分发、校验 SHA-256、
启动时预热，加载过程不下载、不改全局缓存环境、不静默降级。出处与许可证见
`app/tokenizer_data/README.md`。

**已有 .env 必须补充 `RAG_EMBEDDING_MAX_INPUT_TOKENS`，填写当前服务文档规定的单条
输入上限。** 默认 0 表示未知，真实 Embedding 客户端拒绝装配，避免猜测 8192。
可选 `RAG_EMBEDDING_MAX_BATCH_TOKENS` 限制一批的总 token；0 表示未声明。
`RAG_TOKEN_SAFETY_RATIO=0.95` 应用于上述有效上限与聊天总窗口，仅提供余量，
不能保证通用 BPE 与其他模型的实际计数一致。部署应按实际模型使用量校准。

文档按结构切片，使用 Embedding 编码器计数；文件名、标题、正文拼装超限时重新切片，
不静默截断。超长表格/列表/代码原子行或元数据无法容纳时明确失败。最终 Embedding
入口同时校验文档和查询，按条数与总 token 拆批；输入原文保持不变，input_hash 与
实际发送文本一致。查询超长会拒绝，不自动删除用户约束。注入的自定义 Embedding
适配器须自行实现服务端批量限制；索引层仍按 Settings 校验单条最终文本。

聊天、查询改写和来源组装使用聊天计数器。调用前统计角色、内容、工具 schema、
工具调用参数、消息标识，再预留输出和 `RAG_CHAT_TOKEN_OVERHEAD=256`；完整请求需满足
`输入估算 + 输出预留 + 模板余量 <= RAG_INPUT_TOKENS * RAG_TOKEN_SAFETY_RATIO`。
超过预算会拒绝或收缩来源上下文；改写超限回退原查询。BPE token 不能逐段相加，
合并、重叠、围栏、引用装饰后均重新计数，前后缀按 Unicode 字符边界选取并复检。
`token_usage` SSE 事件保存回答调用的估算及模型返回的真实 usage（不可用时 null）；
查询改写的 usage 保存在 `query_plan.query_token_usage`，不将估算用作真实计费值。

切片版本包含编码表版本、结构算法、大小、重叠、解析和 Embedding 预算参数。
升级后需显式调用 `POST /api/v1/knowledge-bases/default/index`：旧版切片会重新解析并
重建向量，chunk_id 会变化；旧版本资料不参与当前检索。启动仅迁移元数据，不调用
外部模型；仅切换聊天编码器不会触发文档重建。当前任务图为 chat-rag-v6；
不兼容的旧图未完成任务不能直接恢复，历史已完成对话保留。


## 上下文组装第一版

RAG 新任务使用独立的 `assemble_context` 节点：同库历史窗口 → 连续主块合并和精确重叠去重
→ 主要证据优先装箱 → 按问题抽取超长正文 → 剩余预算补邻居 → 最终请求复检。
系统规则与 JSON 证据/原始问题分层传递；知识库回答不绑定通用工具，普通对话保持原有工具流程。
来源面板展示本轮模型实际看到的正文，保留原文范围与组装诊断。

新增 `RAG_HISTORY_TOKENS=1536`、`RAG_HISTORY_TURNS=3`、`RAG_NEIGHBOR_BUDGET_RATIO=0.2`。
完整历史继续保存，但切库后的旧库回答不会进入新库的模型请求或问题改写。
原有资料/输入/输出预算及邻居开关保持原配置；第一版不增加远端模型调用，也无需重建索引。
检索结果和最终组装快照分别持久化，模型恢复复用同一请求。第一版交付时任务图为
`chat-rag-v5`，当前为 `chat-rag-v6`；不兼容的旧版未完成任务需取消后重新提交。
实现边界、配置和验证见 [第一版上下文说明](docs/context-assembly-v1.md)。

## 结构化回答第二版

RAG 回答现使用 `rag-answer-v2` 协议：模型返回段落和来源关联，后端严格校验后生成引用；
前端不再把新回答正文中的年份、数组下标等数字当作引用。未校验的 JSON 不展示，答案校验后一次性发布。
`GET runs`、会话 turns 与 SSE done 增加 `answer_status`、`answer_reason`、`answer_segments`、
`answer_protocol`；状态和来源会持久化，刷新与重启保持一致。

格式错误或非法编号最多进行一次修复，持久化 `answer_repair_calls`，消耗原有模型调用与耗时预算。
默认 `RAG_ANSWER_JSON_MODE=true`；兼容端点不支持 JSON mode 时可关闭，服务端仍严格验证协议。
迁移 `006_structured_answers.sql` 自动执行，无需重建索引。旧 v5 等未完成任务需要取消后重新提交。
实现和验证见 [第二版说明](docs/structured-answer-v2.md)。

## 基础检索实测

当前使用 FastGPT 文本召回流程，默认单查询，不默认启用查询改写/扩展、Reranker 或邻居补全。
历史基础版实测仍保留原始报告；它使用旧分词、扁平融合及向量预过滤，不能代表当前版本质量。

运行真实 Embedding 对比（只发送合成测试文本，使用临时数据库，不需要聊天模型调用）：

```bash
uv run python -m scripts.eval_retrieval
```

固定语料见 `evals/retrieval_baseline.json`。当前报告默认输出到 `evals/results/fastgpt-retrieval.json`，
包含三种模式的逐题排名、Recall@1/3/5、MRR@10、查询向量化延迟和本地搜索延迟。
查询向量预计算并复用，本地搜索耗时不包含远端 Embedding 请求。
基础召回评估显式关闭 Rerank，不受 `.env` 的重排开关影响。
合成样本不代表真实业务质量；混合效果以实测为准。


## FastGPT 文本召回

分词采用 jieba-rs 的 Python 绑定 rjieba 0.1.13，普通 cut/HMM=true，按 FastGPT 顺序去除
Markdown 标记和 URL、清理指定全角字符、过滤同一份停用词。不再做中文双字切分、NFKC、
自定义 HR 词表或编号保护。查询与文档使用同一分析器；文档保留词频，FTS 查询条件去重。

全文为单一词项列，输入为标题与正文；文件名只保留为来源信息，不参与全文匹配。
向量输入和索引版本算法保持兼容，查询仍走模型 embed_query；未增加索引扩展或图片召回。
Python 绑定已对照 FastGPT @node-rs/jieba@2.0.1：90 条实际样例及其词典 349,045 个词的
分词输出一致。快照位于 tests/fixtures/fastgpt_terms.json。停用词出处与许可见
app/rag/term_data/README.md。

**存储适配边界**：FastGPT 非 Milvus 路径使用 Mongo `$text` 的 textScore，Milvus 路径使用
原生 BM25。本项目使用 PostgreSQL 位置倒排表，按原 BM25 参数（k1=1.2、b=0.75）
计算词项/短语频次、目标知识库有效全文版本内的 IDF 和长度归一化，不替换为 ts_rank；固定语料的原始分数对照一致。
不同全文引擎仍可能存在字符规范化等边界差异，不承诺与 Mongo textScore 排序完全相同。

全文版本独立于向量版本。升级后调用 `POST /api/v1/knowledge-bases/default/index`，利用已有
chunks 本地重建；向量和切片有效时不重切片、不调用 Embedding。
全文元数据同时保存其对应的实际 chunk_version；仅更换 Embedding 配置不会屏蔽旧切片的
有效关键词索引，显式重切片时再使旧全文失效。file_ids 只过滤候选，不改变知识库内的 IDF / avgdl。未重建时 keyword 返回
keyword_index_outdated，hybrid 可降级到向量。旧字段权重和候选数量配置已移除。

```bash
uv run python -m scripts.eval_bm25
```

该脚本在相同向量和当前召回流程下比较历史双字词项与当前 FastGPT 分词，报告写入
`evals/results/fastgpt-{baseline,holdout}.json`。不会覆盖历史 bm25 报告；分词对照结果也不能
冒充旧检索流程与新流程的端到端质量比较。Reranker 保持原有可选接口，本次不改造。

## 本机 PostgreSQL 配置与验证

项目连接本机 PostgreSQL，业务数据与检查点保存在数据库中，原始文件由
`RAG_FILE_STORAGE_PATH` 管理。默认业务库为 `langchain_rag`，专用测试库为
`langchain_rag_test`，均位于 `127.0.0.1:5432`。

本机 PostgreSQL 与 pgvector 已安装时，启动服务并检查端口：

```bash
brew services start postgresql@16
pg_isready -h 127.0.0.1 -p 5432
```

新机器首次准备数据库时，以本机管理员身份连接 `postgres` 库执行以下 SQL。
当前开发机已完成此步骤，无需重复执行。示例密码与 `.env.example` 一致，可自行替换，
并同步修改 `RAG_DATABASE_URL` 和 `RAG_TEST_DATABASE_URL`：

```sql
CREATE ROLE rag LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'rag_local_dev';
CREATE DATABASE langchain_rag OWNER rag;
CREATE DATABASE langchain_rag_test OWNER rag;
```

再由管理员在两个项目库各安装一次扩展：

```bash
psql -h 127.0.0.1 -d langchain_rag -c 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;'
psql -h 127.0.0.1 -d langchain_rag_test -c 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;'
```

应用账号拥有两个项目库即可，无需超级用户权限。首次配置复制 `.env.example`，
已有 `.env` 保留原值，确认 `RAG_DATABASE_URL` 和 `RAG_DATABASE_POOL_SIZE`。
测试命令从进程环境变量读取 `RAG_TEST_DATABASE_URL`，不会自动读取 `.env` 中的该项。

启动应用时在一个事务内执行 `app/migrations` 的未应用版本；LangGraph PostgreSQL Saver
通过自己的 setup 管理检查点结构。业务事务绑定一个池连接，切片替换时原子失效旧向量与全文。
全文与向量分别以独立事务发布；向量与 indexed 状态一起提交，任一路失败不回滚另一条已成功路径。外部模型调用及大文件写入在事务之外。本地文件读写、删除和孤儿扫描在线程执行；
写入取消时先等待进行中的磁盘操作结束，再关闭文件及清理未完成发布的对象。checkpoint 写入与业务发布仍为
两个阶段，通过已有可恢复发布协议对账。

默认 RAG_VECTOR_SEARCH_MODE=exact，不创建 ANN 索引，执行数据库内精确余弦搜索。
精确检索和任务恢复验证后，可设 RAG_VECTOR_SEARCH_MODE=hnsw，并显式配置实际
RAG_EMBEDDING_DIM（当前 HNSW vector 索引支持 1–2000 维）再重启。
应用会为当前模型版本/维度并发创建独立 HNSW 表达式索引；要求 pgvector >=0.8，查询启用
strict_order 迭代扫描，并通过 RAG_HNSW_EF_SEARCH 调整搜索预算（默认 100）。
该路径已测试范围过滤、与精确参考结果对照，并通过 EXPLAIN 验证索引可用；小样本一致
不代表任意语料零召回损失。切回 exact 无需删除索引，精确查询不使用 HNSW 表达式。
切换搜索模式不修改向量输入版本，不需要重新调用 Embedding。

测试必须显式设置专用 PostgreSQL 地址：

```bash
RAG_TEST_DATABASE_URL=postgresql://rag:rag_local_dev@127.0.0.1:5432/langchain_rag_test uv run pytest -q
uv run ruff check .
```

每个测试使用随机 schema，结束后只删除自己的 schema。模型/Embedding 使用测试替身；
PostgreSQL、pgvector 和 checkpoint 是真实实现。评估与 smoke_embedding 脚本也使用独立
随机 schema，结束后清理，不写入正常知识库；运行前需准备专用测试数据库。

使用 `.env` 中真实服务验证完整链路（要求启用 Rerank，并准备 langchain_rag_test）：

```bash
uv run python -m scripts.smoke_rag
```

脚本只在测试库随机 schema 中上传四种合成文件，验证真实 Embedding、三种检索模式与
Rerank、空表头值、带引用的真实问答、SSE、历史和重启持久化；结束后清理临时数据。
结果保存到 `evals/results/rag-e2e-smoke.json`，不包含服务密钥。

## 简单可观测性

启动后访问 `GET /api/v1/metrics` 查看当前进程的 JSON 累计指标，不需要新增配置或服务。
正常及已处理错误的业务 HTTP 响应携带服务器生成的 `X-Request-ID`，并输出一行 JSON 请求日志
（request_id、路由模板、方法、状态码、耗时）。日志与指标不记录请求正文、查询参数、
文件名或 API Key；路径 ID 以模板归并，未知路径统一记为 unmatched。

- `timings`：HTTP（SSE 包含完整连接时间）、解析、切片、全文与向量索引、文档 Embedding、
  向量/关键词召回、融合、邻居补全、Rerank、查询规划和回答模型的次数、均值、最大值、固定累计耗时桶。
- `active`：使用 span 的阶段当前执行数；vector_index / fusion / rerank 仅提供耗时统计。
- `retrieval_results`：empty / nonempty / error；`retrieval_degradations`：成功返回时的各类降级次数。
- `rerank_http_attempts` / `rerank_http_retries`：真实重排 HTTP 尝试及重试次数。
- `tokens_estimated_input` 与 `tokens_actual_input/output`：本地估算与服务商实际用量分开累计，
  stage 为 embedding_documents、embedding_query、rerank、query_plan 或 answer。Embedding 接口
  目前未返回 usage，因此只提供估算；重排重试的估算按每次请求累计，实际量只累计服务商返回的 usage。

例如用 `curl http://127.0.0.1:8000/api/v1/metrics` 查看。指标自身不计入 HTTP 指标，
进程重启会清零；累计桶可供后续接入监控系统，本版不提供持久化、跨进程聚合或精确分位数。
同阶段 outcome=error 的次数 / 该阶段总次数可计算失败率；降级次数 / 成功检索次数可计算降级率。
单次请求仍可通过 search.diagnostics 查看各阶段耗时与降级原因。

升级包含 003_independent_lexical.sql，启动时自动执行结构迁移；已有全文索引需显式调用
`POST /api/v1/knowledge-bases/default/index` 重建。本地全文重建本身不调用外部模型；
同次 index 如发现向量/切片过期，仍会照常处理相应阶段。引用优化设计见
[结构化回答第二版](docs/structured-answer-v2.md)，当前已使用结构化来源关联与显式资料不足状态。
