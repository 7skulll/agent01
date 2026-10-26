# 模型设置

更新：2026-10-26。侧栏「模型设置」可配置工作空间共用的对话模型与 Embedding。
整页仅管理员可见；同一页面下方还有「用户管理」面板，管理账号与角色（接口前缀 `/api/v1/users`）。

## 使用

1. 对话模型选择 OpenAI 兼容或 DeepSeek 协议，填写 Base URL、模型 ID 和 API Key。
   服务地址应包含服务商要求的版本路径，不要填写具体的 `/chat/completions` 接口路径。
2. 设置上下文窗口和最大输出 Token，按实际模型限制填写。普通对话需要工具调用支持；
   知识库问答默认使用 JSON mode，兼容服务不支持时仍需通过环境配置关闭 JSON mode。
3. Embedding 使用 OpenAI 兼容 Embeddings API，填写其独立的服务地址、模型 ID 和 API Key。
   单条输入 Token 上限必须明确填写；向量维度 0 表示首次调用推断，填写正数表示校验期望维度，
   不等同于请求服务商缩短向量。向量检索固定使用 pgvector 精确余弦，
   不构建 HNSW 等近似索引，因此**没有维度上限**（迁移 `013_drop_hnsw_indexes.sql` 已移除近似索引）。
4. 分别点击保存。保存后立即生效，重启后保留。保存不发送外部请求，也不自动重建索引；
   页面“已配置”仅表示已装配客户端，不表示鉴权、连通性或接口能力已验证。

API Key 留空或省略时保留该模型现有密钥；首次配置必须提供密钥。
“恢复环境配置”移除该模型的界面覆盖，恢复本次服务启动时的环境配置，另一类模型保持不变。
模型 ID 需由服务商提供；此功能不自动枚举远端模型。

## 保存与切换

配置覆盖保存在 PostgreSQL 的 model_settings 表中，优先于 `.env`，不修改 `.env`。
API Key 是服务端数据库中的凭据，当前未做应用层加密；数据库备份同样包含这些凭据。
GET/保存/恢复接口只返回 key_configured 状态，不回传密钥，也不把密钥放入浏览器存储、
问答状态、SSE 或检查点。设置响应带 Cache-Control: no-store，界面保存成功后清空密码框。

当前应用是工作空间共用的单 worker 服务，这不是按用户隔离的账户密钥管理。
正在执行或等待恢复的问答、暂停任务和正在运行的索引会阻止配置切换，返回 409。
对话模型切换后重建运行图；切片、向量、检索服务使用一致的新配置。

切换 Embedding 模型、地址、维度或相关输入预算可能改变索引版本。旧向量不会作为新模型
的向量参与检索；请到知识库点击“构建 / 重试索引”。全文索引保持独立可用。
只轮换 API Key 且其他字段不变不会改变向量索引版本。切换聊天模型不重建文档向量。

## 接口

路径前缀 `/api/v1/settings/models`：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | 空 | 当前公开配置及密钥是否已设置 |
| PUT | /llm | 保存 provider、model、base_url、可选 api_key、max_tokens、input_tokens |
| PUT | /embedding | 保存 model、base_url、可选 api_key、dimension、max_input_tokens |
| DELETE | /{kind} | `kind` 为 `llm` 或 `embedding`，恢复对应环境配置 |

以上四个接口都要求管理员（`require_admin`）。

迁移 `008_model_settings.sql` 启动时自动执行，仅添加配置存储，无需重新上传既有文件。
环境变量 `RAG_LLM_PROVIDER` 可选 `deepseek`（兼容旧行为）或 `openai`；后者使用 Chat Completions。

### 用户管理

同一页面下方的用户管理面板使用另一个前缀 `/api/v1/users`：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | 空 | 列出账号 |
| POST | 空 | 创建账号，指定用户名、显示名、角色、密码 |
| PATCH | /{user_id} | 改角色、停用、重置密码 |
| DELETE | /{user_id} | 删除账号 |

三个角色（`app/users.py` 的 `ROLES`）：`admin`（管理员，全部权限）、`staff`（员工，上传/删除文件、构建索引、管理知识库、发起与审核评测）、
`viewer`（普通用户，只读，可问答、检索、下载）。账号与会话表由迁移 `014_users.sql` 创建；
自助注册固定创建 `viewer`，可用 `RAG_REGISTRATION_ENABLED=false` 关闭。

服务端命令行管理见 [项目 README](../README.md) 的 `scripts.user_admin` 部分。
