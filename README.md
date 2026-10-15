# enterprise-langchain-rag

企业知识库 RAG 学习项目 · FastAPI + LangChain v1 + LangGraph + SQLite

当前已完成 **Agent 可恢复任务、文件解析切片、Embedding 向量化、SQLite 向量持久化、
FTS5 混合检索与带引用的知识库问答**。

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
- 文件上传：multipart 单文件（50 MiB 硬限）、扩展名白名单、文件名安全、
  `Idempotency-Key` 幂等、SHA-256 内容哈希、本地对象存储（临时文件 + 硬链接原子发布）
- 文件解析与切片：`.txt/.md/.pdf/.docx/.xlsx` → 结构化 Block → 递归切片 + 重叠，
  切片落库并推进状态机
- 向量化：批处理、有限重试、数量/维度/有限值校验，文件向量与 indexed 状态原子提交
- 检索：精确余弦召回 + FTS5 词项召回 → RRF 排名融合 → 内容去重
- 知识库问答：显式检索节点、上下文预算、引用编号校验、来源持久化与 SSE 输出

尚未实现（后续阶段）：

- 实际 Rerank 服务适配、评估集与阈值校准
- 大规模 ANN 向量索引
- 文件下载/删除、孤儿文件 GC、多知识库

明确不做：OCR、图片处理、表格数值计算。

## 快速开始

```bash
uv sync
cp .env.example .env      # 首次配置，填写 LLM、Embedding 及其单条 token 上限；已有 .env 时保留原配置
uv run uvicorn app.main:app --reload
```

API 文档：<http://127.0.0.1:8000/docs>

### 环境变量（`.env`，前缀 `RAG_`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `RAG_LLM_MODEL` / `RAG_LLM_BASE_URL` / `RAG_LLM_API_KEY` | 模型配置 | 空 |
| `RAG_EMBEDDING_MODEL` / `RAG_EMBEDDING_BASE_URL` / `RAG_EMBEDDING_API_KEY` | Embedding 配置 | 空 |
| `RAG_EMBEDDING_DIM` | 期望维度，0 为首次调用推断 | `0` |
| `RAG_EMBEDDING_BATCH_SIZE` / `RAG_EMBEDDING_MAX_RETRIES` | 批大小、最大重试次数 | `10` / `3` |
| `RAG_CONTEXT_TOKENS` / `RAG_INPUT_TOKENS` | 资料预算、总输入及输出预留预算（估算 token） | `2048` / `8192` |
| `RAG_CHAT_DATABASE_PATH` | SQLite 路径 | `data/chat.sqlite3` |
| `RAG_FILE_STORAGE_PATH` | 本地对象存储根目录 | `data/files` |
| `RAG_CHUNK_SIZE_TOKENS` / `RAG_CHUNK_OVERLAP_TOKENS` | 切片大小与重叠（配置的 tokenizer 单位） | `512` / `64` |

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
    indexing.py      # chunks 表 + 解析/向量化批量编排
    embedding.py     # OpenAI 兼容 Embedding 客户端
    vectors.py       # SQLite 向量、索引版本与原子发布
    retrieval.py     # 独立搜索接口、RRF 与来源预算
    terms.py         # 索引/查询共用词项规则
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
| `POST /knowledge-bases/default/index` | 批量解析与向量化、重试及模型变更重建（202） |
| `POST /knowledge-bases/default/search` | 独立向量或混合检索 |

知识库当前仅支持 `default`，其他返回 404。

## 开发

```bash
uv run pytest -q          # 测试
uv run ruff check .       # 静态检查
uv run ruff format --check .
```

## 索引与问答使用

上传文件后调用 `POST /api/v1/knowledge-bases/default/index`，轮询文件列表。
配置 Embedding 后状态流为 `not_indexed → parsing → recorded → indexing → indexed`；
未配置时停在 recorded。解析失败为 failed；向量化失败回到 recorded 并写入 index_error，
再次调用 index 会复用原 chunks。重启回收 indexing 状态，不自动执行外部模型调用。

chunk_vectors 保存 chunk_id、文件 ID、模型、维度、索引版本、实际输入 SHA-256 与向量。
索引版本包含模型、服务地址、配置维度、归一化设置和标题拼装版本；配置变化后调用 index
会重建向量，保持 chunk_id。检索只读取 indexed 且属于当前版本的资料。
原始正文单独保存，Embedding 输入为文件名、标题路径和正文。

独立检索请求：

```http
POST /api/v1/knowledge-bases/default/search
Content-Type: application/json

{"query":"员工年假有多少天？","mode":"hybrid","top_k":5,"min_score":0.2}
```

mode 支持 vector/hybrid/keyword。两路召回独立并行，过滤 indexed 状态、当前索引版本和
file_ids 后才截取候选。纯关键词模式不调用 Embedding；未配置 Embedding 时可搜索已经
indexed 的文件。混合模式可在一路失败时降级，diagnostics 标记原因；降级后仍无候选时
报告检索不可用，不把服务故障解释成“没有资料”。

可添加最多两个扩展 queries 和可选 file_ids；空 file_ids 表示不搜索任何文件。例如：

```json
{"query":"年假审批","queries":["带薪年假申请流程"],"file_ids":["文件ID"],"mode":"hybrid"}
```

按 FastGPT 模式分配候选：混合模式每问题向量 80、全文 60；纯向量或纯全文模式各 100。
多查询先在向量/全文路内分别做等权 RRF 并截回该路预算，再做两路加权 RRF（k=60，
默认各 0.5）。空路、零权重路不参与；仅剩一路时保留其顺序和分数，不额外套 RRF。
min_score 仅在纯向量模式对最终候选生效，混合与全文模式不按它过滤。
去重按标题+正文的字母/数字归一化，并保留 alternatives 来源。
每文件上限默认 20，与接口 top_k 上限一致。向量仍为 SQLite 精确余弦扫描，多查询共用
一次 JSON 解码和归一化；后端没有迁移为 MongoDB、PostgreSQL 或 Milvus。

sources 保留 channels、matched_queries、vector_score、keyword_score、rerank_score，
各分数分别表示其原始度量；未从对应通道命中时为 null，不充当可信概率。
diagnostics 包含候选数量、扫描数量、各阶段耗时、降级原因和是否应用重排。

可选 Reranker 通过 `create_app(reranker=adapter)` 注入。适配器实现异步
`rerank(query, documents) -> list[float]`，分数须与输入文档一一对应，且全部为有限值。
输入为文件名、标题和自然正文，模型窗口处理由适配器负责。默认重排前 40 个候选，
按原排序与重排名次融合，权重 0.75；10 秒超时、数量错误或无效分数均回退原排序。
当前未配置实际重排服务，不新增外部依赖。

邻居扩展默认关闭；显式配置后，选中正文可回查相邻切片，只允许同文件、同标题、同页、当前版本的正文，
补充片段保留 supporting_chunks。所有来源、补充片段和引用装饰计入上下文预算。
正文首条超长时按预算裁剪并标注 truncated；结构化内容不截断行或代码围栏。

提交知识库问答：

```http
POST /api/v1/conversations/{conversation_id}/runs
Content-Type: application/json

{"message":"员工年假有多少天？","idempotency_key":"leave-001","knowledge_base_id":"default"}
```

不指定 knowledge_base_id 时保持普通 Agent 对话。知识库问答在模型调用前检索，
基础版默认直接使用原问题；显式开启追问补全后，最多使用最近 6 条、1000 个估算 token 的历史。
原问题始终保留，JSON 无效或 10 秒超时则回退。可配置 RAG_QUERY_EXPAND=true
允许额外一条表达，连同原问题总共最多 3 个查询。规划调用消耗持久化模型调用预算。
查询计划和资料分别保存到检查点，恢复失败的模型节点时复用两者。空资料返回不足提示。
问答先取最多 10 条结果，再按总输入及 2048 token 的资料预算挑选。
答案必须使用本轮合法的 `[1]` 等引用编号；非法或缺失引用不发布成功历史。
GET runs 和 SSE done 事件包含实际被引用的 sources；SSE 另有 query_plan、retrieval_start/retrieval_end，后者包含检索诊断。
引用校验保证编号对应本轮资料，不能证明答案每个事实都受资料支持。

## 验证与边界

```bash
uv run python -m scripts.smoke_embedding  # 少量真实 API 调用，合成资料和临时数据库
```

health 的 embedding_ready 表示客户端配置已装配，不表示远端鉴权或网络验证通过。
运行保持单进程单 worker；SQLite JSON 向量做精确扫描，适用于当前学习项目与小型知识库。
上下文 token 是估算值，应根据模型窗口设置预算；超长结构化行会明确失败。
当前未接入真实 Rerank 服务和答案质量评估；追问改写为可选能力，默认关闭。
任务图版本为 chat-rag-v4，旧版本未完成任务不支持直接恢复。

当前链路为原问题（可选扩展）→ 全文/精确余弦并行召回 → 路内多查询 RRF → 两路加权 RRF → 去重与 top_k。
查询改写、查询扩展、邻居补全默认关闭，未注入 Reranker 时不做精排。已有可选能力保留，
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
外部模型；仅切换聊天编码器不会触发文档重建。新任务图为 chat-rag-v4，旧图未完成
任务不能直接恢复，历史已完成对话保留。


## 基础检索实测

当前使用 FastGPT 文本召回流程，默认单查询，不默认启用查询改写/扩展、Reranker 或邻居补全。
历史基础版实测仍保留原始报告；它使用旧分词、扁平融合及向量预过滤，不能代表当前版本质量。

运行真实 Embedding 对比（只发送合成测试文本，使用临时数据库，不需要聊天模型调用）：

```bash
uv run python -m scripts.eval_retrieval
```

固定语料见 `evals/retrieval_baseline.json`。当前报告默认输出到 `evals/results/fastgpt-retrieval.json`，
包含三种模式的逐题排名、Recall@1/3/5、MRR@10、查询向量化延迟和本地搜索延迟。
查询向量预计算并复用，本地搜索耗时不包含远端 Embedding 请求。测试说明和本次结果见
[基础版评估](docs/retrieval-baseline.md)。合成样本不代表真实业务质量；混合效果以实测为准。


## FastGPT 文本召回

分词采用 jieba-rs 的 Python 绑定 rjieba 0.1.13，普通 cut/HMM=true，按 FastGPT 顺序去除
Markdown 标记和 URL、清理指定全角字符、过滤同一份停用词。不再做中文双字切分、NFKC、
自定义 HR 词表或编号保护。查询与文档使用同一分析器；文档保留词频，FTS 查询条件去重。

全文为单一词项列，输入为标题与正文；文件名只保留为来源信息，不参与全文匹配。
向量输入和已有向量版本保持兼容，查询仍走模型 embed_query；未增加索引扩展或图片召回。
Python 绑定已对照 FastGPT @node-rs/jieba@2.0.1：90 条实际样例及其词典 349,045 个词的
分词输出一致。快照位于 tests/fixtures/fastgpt_terms.json。停用词出处与许可见
app/rag/term_data/README.md。

**存储适配边界**：FastGPT 非 Milvus 路径使用 Mongo `$text` 的 textScore，Milvus 路径使用
原生 BM25。本项目保持 SQLite FTS5 BM25；流程和分词对齐，不承诺不同全文引擎排序完全相同。
向量引擎也保留现有精确余弦实现，未引入新的数据库部署要求。

全文版本独立于向量版本。升级后调用 `POST /api/v1/knowledge-bases/default/index`，利用已有
chunks 本地重建；向量有效时不重切片、不调用 Embedding。未重建时 keyword 返回
keyword_index_outdated，hybrid 可降级到向量。旧字段权重和候选数量配置已移除。

```bash
uv run python -m scripts.eval_bm25
```

该脚本在相同向量和当前召回流程下比较历史双字词项与当前 FastGPT 分词，报告写入
`evals/results/fastgpt-{baseline,holdout}.json`。不会覆盖历史 bm25 报告；分词对照结果也不能
冒充旧检索流程与新流程的端到端质量比较。Reranker 保持原有可选接口，本次不改造。
