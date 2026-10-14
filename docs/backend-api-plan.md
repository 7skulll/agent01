# 文件上传与解析模块契约（已实现）

当前实现真实文件上传、记录持久化，以及解析、切片和切片落库。
尚不进行向量化与检索：`recorded` 表示切片已就绪、等待后续索引。

## 接口

`POST /api/v1/knowledge-bases/default/files`：multipart 请求，唯一字段 `file`；
必填请求头 `Idempotency-Key`（1–128 个 ASCII 字符）。成功返回 201 和 FileRecord。
默认知识库 ID 为 `default`，其他知识库返回 404。

单文件硬限 20 MiB，允许 `.pdf/.doc/.docx/.md/.txt/.xls/.xlsx`，扩展名大小写不敏感。
空文件返回 422，超限返回 413，非法扩展名返回 415。
multipart 必须完整结束，仅接受一个文件。接收期间即检查限额，额外 multipart 开销限 64 KiB。

文件名先进行 UTF-8 百分号解码、basename（含 Windows 分隔符）、NFC 规范化，
允许 Unicode 字母、数字、组合标记、空格及 `. _ - ( ) （ ）`；最终 UTF-8 长度最多 255 字节。
同名文件可以重复上传。相同键与相同安全文件名、内容哈希返回原记录；不一致返回 409。
内容哈希只是普通索引，不强制内容去重。

## files 表

字段：`file_id`、`kb_id`、`name`、`extension`、`size`、`content_hash`（SHA-256）、
`storage_path`、`storage_backend`、`index_status`、`index_error`、
`created_at`、`updated_at`、`idempotency_key`。

索引：非唯一 `(kb_id, name)` 和 `content_hash`；唯一 `(kb_id, idempotency_key)` 和 `storage_path`。
状态允许 `not_indexed/recorded/parsing/indexing/indexed/failed`，上传后为 `not_indexed`。
`recorded` 保留为合法状态；本模块不触发后续状态变化，`index_error` 初始为 null。

`storage_path` 为相对于存储根目录的对象键，如 `default/{file_id}.pdf`；
`storage_backend=local`，根目录由 `RAG_FILE_STORAGE_PATH` 配置，默认 `data/files`。

## 解析接口

`POST /api/v1/knowledge-bases/default/index`：批量触发该知识库下所有
`not_indexed` 或 `failed` 文件的解析与切片。成功返回 202 和本次调度的
`file_id` 列表；无待处理文件时 `count=0`。同一知识库已有批量任务运行时
返回 409 `index_in_progress`。任务在后台串行处理，单文件失败不影响其他文件。

`GET /api/v1/knowledge-bases/default/files`：分页返回文件记录与解析状态，
字段包含 `index_status`、`index_error`、`chunk_count`，支持 `limit`（1–100，默认 50）
和 `offset`。

## chunks 表

字段：`chunk_id`、`kb_id`、`file_id`、`chunk_index`、`content`、`tokens`、
`heading`、`kind`（`text`/`table`）、`page`、`created_at`。
唯一 `(file_id, chunk_index)`，索引 `file_id`。重解析在同一事务内先删后插。

## 解析与切片

- 支持 `.txt`、`.md`、`.pdf`、`.docx`、`.xlsx`；`.doc`、`.xls` 旧格式明确失败。
- PDF 无文本层（扫描件）视为失败，不产生空切片。
- 表格解析时把表头拼进每一行；Word 标题样式与 Markdown 标题生成 `heading` 路径。
- 切片先按结构（标题/表格）分组，再按估算 token 递归切分，重叠 `chunk_overlap_tokens`。
- token 数为确定性估算（CJK 记 1，其余按 4 字符记 1），不依赖外部 tokenizer。
- `RAG_CHUNK_SIZE_TOKENS`（默认 512）与 `RAG_CHUNK_OVERLAP_TOKENS`（默认 64）可配置。

状态流：`not_indexed` --解析中--> `parsing` --成功--> `recorded`；失败置
`failed` 并写入 `index_error`。进程中断遗留的 `parsing` 在启动时重置为 `not_indexed`。

## 一致性与边界

LocalStorage 提供异步 put/get/delete/exists。文件写入临时对象，完成后以硬链接原子发布，
不覆盖现有对象；失败和取消清理临时文件。文件名仅用于展示，实际存储路径使用后端生成的 UUID。
数据库写入失败回滚事务并删除已发布文件。并发幂等提交复用会话仓库事务锁。
完整请求接收后，发布与补偿在取消屏蔽区完成，避免记录和对象仅完成一侧。
接收阶段断开不创建文件记录，关闭 multipart 临时文件。

本次没有实现文件下载/删除接口、知识库 CRUD、100 文件数量限制、向量化与检索。
进程被强制杀死或断电时，文件系统与 SQLite 无法提供跨系统原子事务；
这类崩溃后的孤儿文件扫描属于后续维护能力。

## 验证

测试覆盖格式白名单、中文和路径清理、UTF-8 字节长度边界、空文件、20 MiB 边界、
超限、同名重复、内容级幂等冲突、并发重试、重启持久化、写盘失败、数据库失败、
不完整 multipart、接收断开和取消、存储对象清理。

解析测试覆盖五种格式、表格表头、标题路径、扫描件与损坏文件失败、批量失败隔离、
重复触发幂等、重解析替换切片、运行中冲突、列表状态、启动对账和切片大小/重叠。
