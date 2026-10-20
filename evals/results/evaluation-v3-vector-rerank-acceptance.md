# RAG 小样本验收报告

实验：d62aed62-ba5f-4a22-b148-02b61e0d6144 · 状态：completed
知识库：default · rag / vector · K=5
资料与配置指纹：2675176f3842c0db15c8ff34f72ae3de6b32ba9765c5d66e88a56fa132b3bea9
案例集元数据：{'name': '真实链路合成题-待人工审核', 'tags': [], 'split': 'development', 'frozen': False, 'version': 1, 'case_set_id': '6c36c967-34cd-4231-9471-27aaf99646db', 'fingerprint': '126625758bb76d6a1aee2c6398e48a1d114e294da90b238bcc5dd24e6f5eb3ab', 'review_status': 'draft'}
实验参数：{'rerank': 'on', 'context_tokens': 512}
业务标签筛选：全部 · 复用成功题：0

## 检索指标

| 指标 | 值 |
|---|---|
| precision@5 | 20.0% |
| recall@5 | 100.0% |
| hit@5 | 100.0% |
| mrr@5 | 100.0% |
| ndcg@5 | 100.0% |

完整标注计分题：1（失败计零）；小样本不推断生产准确率或显著性。

## 人工回答质量

总评：{'pending': 2}；通过率 未评分。待审核/不适用不充当通过。
- fact_coverage：未评分，已审核 0，待审核 2，不适用 0。
- unsupported_claims：未评分，已审核 0，待审核 2，不适用 0。
- conditions：未评分，已审核 0，待审核 2，不适用 0。
- citation_support：未评分，已审核 0，待审核 2，不适用 0。
- refusal：未评分，已审核 0，待审核 2，不适用 0。

实际用量（仅本次执行已知部分）：{'known_tokens': 908, 'known_queries': 2, 'unknown_queries': 0, 'new_model_calls': 2}
必要证据完整进入上下文：1/1；正确拒答（已完成）：1/1；正确拒答（含失败）：1/1。

## 逐题结果与问题

| 问题 | 状态 | 人工总评 | 归因/备注 |
|---|---|---|---|
| 员工每年有多少天带薪年假，谁审批？ | completed | pending |  自动验收流程验证，回答仍待人工审核。  |
| 员工生日礼金的精确金额是多少？ | completed | pending |    |

## 改进与验收边界

先复核失败与未审核案例，再按归因检查召回、重排和上下文裁剪。原文包含不等于语义支持，人工通过不等于完整业务验收。
未核验业务案例、草稿案例集与项目文档样例均不能作为生产签收结论。
