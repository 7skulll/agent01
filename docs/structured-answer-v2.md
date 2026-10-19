# 第二版：结构化回答、引用与资料不足

日期：2026-10-17。任务图 `chat-rag-v6`，回答协议 `rag-answer-v2`。
第一版的组装算法、历史窗口和来源快照继续使用；无需重建任何文件索引。

## 用户可见行为

- 知识库回答校验成功后一次性发布，生成期间显示进度；不展示模型原始 JSON 或修复前草稿。
- 引用按钮只来自服务端校验后的 source_ids，正文中的年份 `[2026]`、`array[3]`、
  普通编号或代码不再被扫描为引用。复制/API 的 answer 仍为带服务端引用标记的 Markdown 字符串。
- 资料缺少所需事实或存在无法确定的冲突时，任务正常完成并显示“资料不足”，不产生伪造引用。
- 普通聊天和工具调用仍使用原有流式流程；历史旧回答继续采用兼容展示。

## 模型协议

```json
{
  "status": "answered",
  "segments": [
    {"text": "公司员工每年享有十天带薪年假。", "source_ids": [1]},
    {"text": "申请需要直属主管审批。", "source_ids": [1]}
  ],
  "reason": null
}
```

资料不足时 status 为 insufficient_evidence，segments 必须为空，reason 仅允许
missing_fact / conflicting_sources；服务端生成稳定的提示文本。

严格校验如下：

- 字段不允许额外项；JSON 重复键、代码围栏、NaN、无效 Unicode 和危险控制字符均拒绝。
- answered 至少一段、reason 必须为 null；每段 text 非空，最多 6000 字；
  最多 32 段，总正文最多 24000 字。原始 JSON 响应最多 64000 字符。
- source_ids 每段 1–20 项，只允许严格正整数，不接受 bool、浮点或字符串；重复项去重。
- 每个 ID 必须属于本轮最终送入模型的 rag_sources，不能引用已裁掉的候选或不存在的编号。
- 最终 answer、answer_segments、实际使用的 sources 由同一次校验结果生成，保持映射一致。
  正文数字不参与来源推断，前端也不对新协议正文执行数字正则替换。

默认通过模型的 JSON mode 请求输出合法 JSON 对象，之后执行服务端 Pydantic 校验。
这不是供应商侧的严格 JSON Schema。兼容端点不支持 JSON mode 时，可设置
`RAG_ANSWER_JSON_MODE=false`，改用提示词约束，服务端校验规则保持一致。
不自动吞掉供应商拒绝请求的错误或静默额外调用模型进行协议探测。

## 图、修复与恢复

```text
检索 → 组装与固定来源 → RAG 模型 → 严格校验 → 发布
                                     ↓ 首次协议/来源错误
                               保存修复消息与状态
                                     ↓
                              最多一次修复模型调用
                                     ↓
                              校验成功发布 / 明确失败
```

原始模型响应存入私有 checkpoint，不进入会话 messages、HTTP 正式结果或 text_delta。
校验后的回答才追加为 AIMessage；JSON 原文不参与后续会话历史。
修复只在 invalid_answer_schema / invalid_citations 时发起，不重查资料、不修改来源集合。
修复消息复用相同的资料和原问题，只增加安全错误码及格式要求，不回显错误原文。
组装时为修复提示预留估算空间，修复前完整请求再次复检。

answer_repair_calls 与总 model_calls 在同一数据库事务中预留，修复调用最多为 1；
它同样消耗模型调用上限、Token 计量和任务总耗时。正常回答不额外多调用一次模型。
两次协议校验都失败时保留 invalid_answer_schema / invalid_citations，不能自动降级为资料不足。
网络故障、模型截断、任务超时和调用预算耗尽保持各自错误语义。

检索、组装、原始响应、修复准备、校验结果分别有检查点：

- 已保存的模型响应可在暂停/重启后继续校验，无需重新调用模型或检索。
- 修复调用的名额一旦预留，即使该调用中断也不会在恢复时再次发起，避免重复或未知的外部请求。
- 已完成图但业务发布中断时，启动对账复用校验结果发布 answer_status 等所有字段。
- 已消耗修复名额的供应商故障不能继续修复，提示重新提问。

## 持久化与 API

迁移 `006_structured_answers.sql` 给 runs 增加：

| 字段 | 含义 |
|---|---|
| answer_status | answered / insufficient_evidence；未完成、普通聊天及旧记录为 null |
| answer_reason | missing_fact / conflicting_sources / no_retrieval_hits / no_context_fit |
| answer_segments | 经校验的 text + source_ids 数组，默认 [] |
| answer_protocol | 新 RAG 结果为 rag-answer-v2，普通聊天和旧结果为 null |
| answer_repair_calls | 持久化修复调用预留次数，范围 0–1 |

GET runs、GET conversation turns、SSE done 均返回状态及结构化结果。
结果与 sources、会话发布和 done 事件在同一业务事务中写入；不靠浏览器内存维持状态。
没有检索结果或没有可装入的资料时，不调用回答模型，分别保存 no_retrieval_hits / no_context_fit。

新增 SSE 进度事件 answer_start、answer_repair、answer_validation；旧事件及普通聊天的 text_delta 保留。
进度事件只携带状态、计数和安全错误码；结构化 JSON 在完成校验前不会被当作用户草稿发送。

Metrics 新增 citation_validation 结果计数、citation_repair 调用耗时与 rag_answer_status 分布；
answer_repair 的估算输入和实际 Token 与 answer 分开记录，两者都计入真实模型成本。
源文、错误模型原文和密钥不会新增到请求日志中。

## 验证

- 后端全量回归：581 项通过。
- 新增协议单测 37 项，覆盖严格 ID、截掉的候选、正文数字、重复键、超长结果、非法 Unicode 等。
- 新增数据库链路 14 项，覆盖资料不足、一次修复、模型预算、修复网络故障、
  修复后暂停重启、已校验结果启动对账、原始 JSON 不外泄，以及 JSON mode 开关。
- 前端单元测试 7 项、Chrome 浏览器 14 项、生产构建通过；Ruff 和修改文件格式检查通过。
- 真实 Embedding/Rerank/回答模型链路通过：真实模型返回两段带来源关联的年假答案，未触发修复；
  询问资料中没有的精确生效日期时，返回 missing_fact；两种结果在重启后均正确保留。
  测试仅使用合成文档和测试库隔离 schema，结束后清理。
- 真实服务报告位于 evals/results/answer-v2-e2e-smoke.json。

## 升级范围

服务启动自动执行新迁移，现有文件、切片、向量和全文索引无需重建。
旧回答不根据数字正则反推结构化数据或资料充分性，状态保留 null，历史内容照常可见。
旧 v5 等未完成任务不能恢复到 v6，需取消后重新提交；已完成的同库历史仍可读取。

结构化引用保证“编号属于本轮真实输入资料、正文和来源关联可追踪”。
它不能单独证明每个事实受到资料语义支持。本版没有新增独立 verifier 或真实业务问题集的准确率评估。
第三版父子块、章节与跨页结构恢复仍未实施。
