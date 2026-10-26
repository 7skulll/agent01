# 评测系统独立复测（2026-10-26）

结论：截图中的写放大、单评测限制、强制终止后的 schema 残留和 keyword 模式可执行重排均可复现。但 JSONB 拼接能解决写放大、普通 VACUUM 能直接归还 650 MB、pgvector 扩展是集群级且必须 preload、Metrics 长期累积样本等说法不成立或证据不足。

## 测试范围

- 代码基线：`8607fd2`，开始测试时工作区干净。
- `langchain_rag` 仅执行只读查询，没有运行 VACUUM、修改配置或写入业务记录。
- 写入、评测和故障模拟使用现有 `langchain_rag_test` 中的独立 schema。
- 运行十题检索评测、十题完整 RAG 评测、并发提交及第二 worker 锁验证。
- 回答模型是本地 ScriptModel；重排使用固定 50 ms 延迟的本地适配器。没有外部模型请求和模型费用，不能将此延迟当成真实供应商性能。
- WAL 对比取一个已有真实单题结果，仅在本机复用。报告不保存问题、文档正文、答案或密钥。单题 JSON 为 418,829 字节；三种方案均为十题、每题两次写操作。使用 `EXPLAIN (ANALYZE, WAL, FORMAT JSON)` 归属到具体语句，避免混入其他连接的全局 WAL。
- 健康接口测试是十次请求，不是五分钟压力测试。RSS 是一次采样，不是峰值或整个进程树的 PSS。

原始数值见 [independent-audit-2026-10-26.json](independent-audit-2026-10-26.json)。复测执行器保存在 `/tmp/langchain_rag_audit_20261026.py`。

## 1. 结果存储：问题属实，JSONB 拼接修复建议不成立

当前业务库：

| 项目 | 结果 |
| --- | ---: |
| 评测记录 | 39 |
| 主表 heap | 56 KiB |
| evaluations 总占用（含索引、TOAST） | 736.16 MiB |
| 存活 results 的存储大小合计 | 79.70 MiB |
| 存活 results 转 JSON 文本大小合计 | 155.31 MiB |
| 存活 snapshot 的存储大小合计 | 0.89 MiB |
| 主表 autovacuum_count | 0 |
| TOAST 表 autovacuum_count | 13 |

占用显著高于当前值大小，但差额不能直接认定为待清理死元组：还包含已清理但留在文件内供复用的空间、页和索引开销。没有执行 pgstattuple 精确页内分类。

十题 WAL 对比：

| 写法 | WAL | 相对整数组 |
| --- | ---: | ---: |
| 每次重写整个 results 数组 | 23,349,326 B / 22.27 MiB | 100% |
| JSONB 拼接 pending，再 jsonb_set 单题结果 | 23,349,326 B / 22.27 MiB | 100% |
| 单题一行，写入和更新当前题 | 2,288,728 B / 2.18 MiB | 9.8% |

JSONB 局部修改仍生成新的大字段值；它减少客户端传输不等于减少 TOAST/WAL 写入。本实验逐题存储降低约 90.2% WAL，但不能保证任意语料都低于 2 MB。

实际服务十题检索为 20 次 `results` 更新；十题完整 RAG 为 30 次。RAG 还有保存 run_id 的写入，不能统一按每题两次估算。

普通 VACUUM 后，测试整数组表从 22,282,240 B 变为 22,323,200 B，并未缩小文件。它回收可复用空间，新增 FSM/VM 等页也可能让总大小稍增。生产库没有执行清理。

## 2. 单评测和单 worker：属实，属于当前架构限制

- 阻塞第一条实际检索后提交第二个实验：第一条 202，第二条 409 / `evaluation_busy`。
- 同一数据库、同一 schema 的第二个 worker ownership 被拒绝。
- 当前行为是拒绝提交，并没有可等待的持久化队列。

队列可改善体验，但不等于实现并行。移除 ownership 锁前需要处理任务领取、重启恢复和跨进程停止，避免第二实例将第一实例任务误判为中断。

## 3. 临时 schema：SIGKILL 残留可复现

- 正常退出：清理成功。
- Python 普通异常：清理成功。
- 强杀本测试创建的子进程：`eval_*` schema 留存。
- 父测试随后精确删除了该残留。

这是 finally 无法覆盖强制终止的边界。建议独立测试数据库和带运行所有权/租约的清理机制；启动时按前缀无差别删除 eval_* 可能误删仍在运行的测试。

## 4. keyword 与重排：现象属实，不能据此认定应全面禁止

注入 50 ms 重排适配器，每种模式三次：

- keyword 跟随重排：53.39、56.94、56.39 ms，均执行重排。
- keyword 显式关闭重排：7.82、3.44、3.01 ms，均未执行重排。

该测试证明重排开关与 keyword 召回模式独立，不证明截图中的真实 694 ms 延迟。BM25 召回后重排是有效组合，是否默认跳过应由产品的“纯关键词”语义决定。已有评测参数 `rerank=off` 可以做公平对照。min_score 阈值在重排之后过滤，不能省去重排请求。

## 5. snapshot：重复计算属实，优先级应降低

- 十题检索和十题 RAG 都调用 snapshot 21 次：创建一次，每题执行前后各一次。
- 十题检索本次 snapshot 总计 73.97 ms。
- 独立只读计时十次：51 个 Python 文件代码哈希平均 1.60 ms；220 个文件语料清单查询平均 1.48 ms。
- 截图的 15.4 ms 不能作为固定成本，本次环境没有复现该耗时。
- 当前 knowledge_bases 只有 kb_id/name/created_at，没有 updated_at。建议中的 KB.updated_at 缓存不能直接使用，必须建立覆盖内容与索引变更的 revision。

两次检查有固定实验输入的正确性目的。可以缓存部署代码指纹、引入语料 revision，不能直接删除变更检测。

## 6. 环境影响与内存

- pgvector 通过 CREATE EXTENSION 启用的对象是数据库级，不是集群级；标准安装使用也不要求 shared_preload_libraries。扩展二进制安装位置和 PostgreSQL 实例资源共享是另一回事。
- 本次业务库约 824.23 MiB，三套依赖目录合计约 671.20 MiB，data 约 105.82 MiB。合计约 1.56 GiB 的当前占用，与截图数量级接近；没有安装前基线，不能把它当作本次测试增量。
- 当前监听进程 RSS 采样：两个 Python PID 合计约 95.03 MiB，Vite 约 61.42 MiB。不是截图中的 462 MB，也不代表运行峰值。
- Metrics 在 10,000 次与 110,000 次固定标签记录后，均为 1 个计数键、1 个耗时键、10 个桶；tracemalloc 跟踪增量约 832 B / 864 B。没有逐请求样本累积的证据。
- trace 会增加单题结果和存储体积，可以考虑去重、按需采集和保留期限，但不能由此推出 Metrics 全局内存泄漏。
- 十题 RAG 完成后普通聊天列表仍为 0，评测会话没有进入聊天列表。
- 十次 health 请求均为 200，平均 2.32 ms；没有复核截图中的五分钟压测。

## 建议优先级

1. 逐题结果存储，减少重复保存的 trace 正文；保留可复现证据和现有接口契约。
2. 独立测试库与可识别运行所有权的孤儿资源清理。
3. 根据使用需求实现持久化排队；保留当前单 worker 保护，直到任务调度可以跨进程安全运行。
4. 明确 UI 的关键词召回和重排开关，提供性能/质量对照，不强制取消 BM25 后的重排。
5. 最后优化 snapshot；建立正确版本信号后再缓存。

autovacuum 调优应结合主表和 TOAST 状态；39 行的主表仅将 scale_factor 从 0.2 改到 0.02，触发阈值约从 57.8 变成 50.8，固定 threshold=50 仍然占主导。

## 清理与业务数据复核

结束时业务库仍为 39 个评测、465 个文件、4 个知识库；业务库 eval_* schema 为 0，测试库 audit_* schema 为 0。本次 schema、上传文件及强杀残留均已清理。测试生成的 PostgreSQL WAL 和内部可复用空间不保证立即从文件系统消失。

## 官方资料

- [PostgreSQL TOAST](https://www.postgresql.org/docs/16/storage-toast.html)
- [PostgreSQL VACUUM 与空间回收](https://www.postgresql.org/docs/17/routine-vacuuming.html)
- [PostgreSQL CREATE EXTENSION](https://www.postgresql.org/docs/17/sql-createextension.html)
- [pgvector 按数据库启用](https://github.com/pgvector/pgvector#getting-started)
