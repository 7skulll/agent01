# LangChain RAG 知识库问答

一个可以在本地运行的知识库问答项目。上传文档后，系统会查找相关内容、生成回答，并展示引用来源。

提供 Vue 工作台，支持文件管理、问答、检索调试和效果评测。

## 核心能力

- **登录与角色权限**：管理员、员工、普通用户三种角色；普通用户只能问答、检索和查看资料，员工可上传/删除文件与管理知识库。支持管理员创建账号，也支持访客自助注册普通用户。
- **文档入库**：支持 TXT、Markdown、PDF、DOCX 和 XLSX，自动解析、切片和建立索引。
- **多种检索**：支持关键词、向量和混合检索，可选重排，查看命中文档与排序结果。
- **带来源的回答**：根据知识库资料回答，点击引用查看原文；资料不足时提示无法确认。
- **可恢复任务**：支持暂停、继续和取消，保存会话历史与任务进度。
- **知识库管理**：创建多个知识库，管理文件，也可限定本次问答使用的文件。
- **模型与评测**：在页面配置对话和 Embedding 模型，管理、预览和导入测评集，分层比较检索层与生成层指标，使用可选 RAGAS LLM-as-judge 或人工审核，运行回归门禁并导出报告。

## 工作流程

```text
上传文档 → 解析与切片 → 建立索引
                            ↓
用户提问 → 检索相关片段 → 组装上下文 → 生成回答与引用
```

## 技术栈

| 部分 | 技术 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Vite |
| 后端 | Python、FastAPI |
| 模型与任务编排 | LangChain、LangGraph |
| 数据与检索 | PostgreSQL、pgvector、BM25 |
| 账号与鉴权 | scrypt 密码摘要、数据库会话令牌、角色权限 |

## 快速开始

### 1. 准备环境

需要 Python 3.11–3.13、uv、Node.js 22.12+（或 20.19+），以及已安装 pgvector 扩展的 PostgreSQL。

先创建数据库和连接用户，并在数据库中启用 `vector` 扩展。连接地址与模型配置参考 [.env.example](.env.example)。

### 2. 启动后端

在项目根目录执行：

```bash
uv sync
cp .env.example .env
```

需要 RAGAS LLM-as-judge 生成层评分时，在独立 worker 环境安装：

```bash
cd tools/ragas-worker
uv sync
```

主服务环境不安装 ragas，避免影响检索、问答和部署依赖。

已有 `.env` 时保留原文件。编辑配置，填写 `RAG_DATABASE_URL` 和对话模型的服务地址、模型名称、API Key。

使用向量或混合检索时，还需配置 Embedding，并按服务要求填写 `RAG_EMBEDDING_MAX_INPUT_TOKENS`。也可启动后在“模型设置”页面配置模型；仅使用关键词检索时无需 Embedding。

确认 PostgreSQL 已启动后运行：

```bash
uv run uvicorn app.main:app --reload
```

接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。

### 3. 启动前端

在另一个终端执行：

```bash
cd frontend
npm ci
npm run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。三种进入方式：

- `.env` 配置了 `RAG_ADMIN_PASSWORD`：首次启动自动创建管理员（用户名 `RAG_ADMIN_USERNAME`，默认 `admin`）；
- 没有任何账号且未配置密码：页面进入“创建管理员”初始化流程；
- 已有账号：使用账号登录，或在注册入口自助注册普通用户。

登录后，管理员可在“模型设置 → 用户管理”中把注册用户升级为员工、停用账号或重置密码；员工可以上传文件、构建索引和管理知识库。也可以在服务器上使用命令行管理账号：

```bash
uv run python -m scripts.user_admin list
uv run python -m scripts.user_admin add --username alice --role staff
uv run python -m scripts.user_admin passwd --username boss
```

更多前端配置与部署方式见 [frontend/README.md](frontend/README.md)。

## 项目结构

```text
app/          后端接口、任务管理与 RAG 流程
frontend/     Vue 工作台
scripts/      验证与评测脚本
tests/        后端测试
evals/        评测案例与历史结果
docs/         设计说明（部分文档描述旧版行为）
```

检索排序、评分类型与阈值语义见 [RETRIEVAL.md](RETRIEVAL.md)。

## 开发检查

后端测试需要专用 PostgreSQL 测试库，并在其中启用 pgvector：

```bash
RAG_TEST_DATABASE_URL=你的测试库连接地址 uv run pytest -q
uv run ruff check .
```

前端检查：

```bash
cd frontend
npm test
npm run build
```

## 当前边界

当前后端使用单进程、单 worker。扫描 PDF 暂不支持 OCR，不处理图片，也不执行表格数值计算。引用可以帮助核对原文，回答质量仍需结合实际业务资料评测。

登录状态：第一个账号创建前，本地 API 按匿名管理员放行，便于完成初始化；创建任意账号后立即要求登录。部署到公网时请先完成初始化。

自助注册固定创建普通用户，不能自行选择员工或管理员角色；如需关闭注册，设置 `RAG_REGISTRATION_ENABLED=false`。

会话与问答历史目前仍是工作空间共享，不按用户隔离；区分权限的范围是知识库写入、模型配置和用户管理。脚本场景可设置 `RAG_API_TOKEN` 作为管理员服务令牌。
