# agent01：企业知识库 RAG

agent program · FastAPI + LangChain

本项目独立于相邻的 `ragflow-main`。第一步完成框架与可测试的问答编排，后续接入实际文档、检索存储和云端模型。

## 已确认范围

- 第一版处理 100 多份文档，本地运行，单用户、多个知识库，查询指定知识库。
- 支持目标：PDF、Markdown、Word（`.doc`、`.docx`）和表格文件。表格扩展名暂按 `.xlsx`、`.xls`、`.csv` 规划，尚未实现解析。
- 不处理 OCR、图片；表格只做信息检索，不做精确计算或行级定位。
- 查找信息、连续追问、资料不足时明确说明。来源展示和 PDF 页码展示后续实现，内部保留元数据。
- LLM 计划使用用户指定的 DeepSeek v4.1 Flash，Embedding 使用 Qwen 云端 API。准确模型 ID、平台、接口和凭据待接入时确认；代码没有猜测默认模型 ID。
- 暂不实现网页、文档管理界面和权限。

## 启动

```bash
git clone https://github.com/7skulll/agent01.git
cd agent01
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API 文档：<http://127.0.0.1:8000/docs>。

```bash
curl http://127.0.0.1:8000/api/v1/health
curl -X POST http://127.0.0.1:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"knowledge_base_id":"hr","question":"年假怎么申请？","history":[]}'
```

健康接口包含 `rag_ready` 和 `chat_ready`。RAG 问答接口当前仍返回 HTTP 503，说明检索与模型尚未接入。

## Agent 对话与持久化历史

填写本地 `.env` 中的 `RAG_LLM_MODEL`、`RAG_LLM_BASE_URL`、`RAG_LLM_API_KEY`，然后启动服务或运行下面的测试脚本。请使用服务商提供的准确模型 ID。`.env` 已忽略提交；不要把真实密钥放到 `.env.example`。

独立对话使用 `langchain-deepseek` 与 LangChain `create_agent`。模型可以直接回答，也可以输出工具调用，由框架执行工具并将结果交回模型，循环直到最终回答。当前工具是 `current_time`（北京时间）和 `multiply_numbers`（两数乘法），没有知识库检索工具。

每次请求默认最多 6 次模型调用、8 次工具执行，整体超时 120 秒，SDK 自动重试关闭。可通过 `.env` 配置。超限返回 429，超时返回 504。工具循环通常会产生多次云端调用与相应费用。

首次不传 `thread_id` 会自动创建会话；后续只传同一 `thread_id` 和新消息，框架自动恢复历史。不再接受 `/chat` 的客户端 `history`。响应包含 `answer`、`thread_id`、`tools_used`（本轮实际执行的工具名）。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"请计算12乘7"}'

# 将返回的 thread_id 填入下一次请求。
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"thread_id":"替换为返回的UUID","message":"刚才计算的结果是什么？"}'

# 直接通过 FastAPI 测试客户端调用真实云端模型，无需先启动服务器。
uv run python -m scripts.smoke_chat

# 真实工具调用 + 重开数据库后的历史记忆测试。
uv run python -m scripts.smoke_agent
```

未配置返回 503，上游 API 错误返回 502。脚本属于手动联网测试，不包含在 pytest 离线测试中。已验证的模型连接与工具调用能力需要分别测试：普通对话成功不保证服务支持 tool calling。

会话接口：

- `POST /api/v1/conversations`：新建空会话，返回 UUID。
- `GET /api/v1/conversations?limit=50&offset=0`：分页列出会话。
- `GET /api/v1/conversations/{thread_id}`：读取会话状态、用户/助手消息及工具调用结果。

默认自动创建 `data/chat.sqlite3`，无需启动数据库服务。`conversations` 表保存会话时间与状态；LangGraph 的 SQLite checkpoint 表保存消息和 Agent 状态，包括工具调用与工具结果。两者通过 `thread_id` 关联。服务关闭时释放数据库连接，重启后可继续正常会话。

同一会话执行中再次提交会返回 409。调用失败或进程中断的会话标记为 `failed`，历史可能包含未完成的一轮；当前禁止直接继续，需创建新会话，已有历史仍可查看。这不是自动故障恢复。

当前为本地单用户、单进程版本，启动时仅使用一个 worker。SQLite 适合此阶段；多人部署时需要迁移 PostgreSQL、用户权限和跨实例并发控制。尚未实现历史摘要或上下文裁剪，长会话可能超过模型上下文限制。对话库不存 RAG 向量，后续单独选型检索存储。

集成依据：[LangChain Agent](https://docs.langchain.com/oss/python/langchain/agents)、[短期记忆](https://docs.langchain.com/oss/python/langchain/short-term-memory)、[SQLite 检查点](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/aio/AsyncSqliteSaver)。

## 第一阶段已实现

- FastAPI 应用工厂、生命周期、版本化路由、OpenAPI 文档、请求校验。
- 环境配置；密钥字段使用 `SecretStr`，`.env` 忽略提交。
- LangChain `Document` 切片、提示词、Runnable 模型链和文本输出解析。
- 问答编排：历史消息 → 追问改写 → 按知识库检索 → 上下文 → 回答。
- 检索器接口注入；服务层再次检查知识库标识。无有效片段时直接返回资料不足，不调用回答模型。
- 离线测试通过替代检索器和模型测试编排，不消耗云端 API。

RAG 的 `/query` 仍由调用方传入 `history`，后续与持久化会话整合。该接口没有真实检索器与回答模型；当前“有片段但资料不足”依靠提示词和约定回复识别，还没有检索质量门槛或可靠的证据充分性判定。

## 目录

```text
app/
  main.py       FastAPI 工厂、接口、生命周期
  config.py     环境配置
  schemas.py    问答请求、历史消息、响应
  chat.py       Agent 创建、工具循环、会话历史恢复
  tools.py      时间与乘法工具
  conversations.py  SQLite 会话元数据
  pipeline.py   LangChain 切片、提示词和问答编排
tests/
  test_api.py   API 与编排离线测试
  test_chat.py  Agent、工具执行、历史恢复、循环限制与异常测试
scripts/
  smoke_agent.py  真实云端工具调用与记忆测试
docs/
  rag-flow.md   完整流程及后续开发顺序
```

## 验证

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

离线测试不是端到端质量评估；实际解析、云端服务与检索命中率接入后，需要真实样本文档和标注问题验证。
