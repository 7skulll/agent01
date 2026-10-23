# 文档导航与项目状态

更新：2026-10-19。

## 当前实现

上传、解析切片、独立全文索引、向量化、混合检索、可选真实 Rerank、多知识库、可恢复任务和 Vue 工作台已实现。
上下文组装使用 context-v1；新 RAG 回答使用 rag-answer-v2，任务图为 chat-rag-v6。

- [项目 README](../README.md)：启动、配置、接口和当前边界。
- [评测指标口径](evaluation-metrics.md)：召回率、精准率、MRR、忠实度、正确性与完整性的严格计算方式。
- [上下文组装第一版](context-assembly-v1.md)：组装算法、预算与来源快照；历史版本说明。
- [结构化回答第二版](structured-answer-v2.md)：当前回答协议、引用、资料不足与恢复。
- [前端验证记录](frontend-validation.md)：分阶段交付及验证证据。
- [模型设置](model-settings.md)：自有 API Key、协议、保存与切换规则。
- [日常管理](daily-management.md)：文件下载/删除、上传残留清理、会话管理及问答文件范围。
- [重排审计第二版](rerank-audit.md)：全量合成集、分组指标与复现方法。
- [同页 RAG 评测第三版](evaluation-v3.md)：当前指标口径、证据追踪、案例集、审核、重试与归档。
- [同页 RAG 评测第一版](evaluation-v1.md)：历史指标、曲线与操作口径。
- [评测第二版规划](evaluation-v2-plan.md)：证据标注、实验对比与回答审核的历史规划。
- [后续路线图](context-assembly-plan.md)：尚未实施的质量评估和上下文第三版。

## 文档整理

已完成的前端实施计划和引用校验计划已合并到实现说明及路线图，不再保留两套当前状态。
旧 BM25 与检索基线说明描述已替换的实现，不作为现行文档；历史实验 JSON 保留在 evals/results。
历史验证记录中的测试数量是对应版本的快照，不代表当前版本的测试总数。
