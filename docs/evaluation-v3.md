# 同页 RAG 评测第三版

更新：2026-10-26。当前实现为第三版，是评测口径的唯一现行说明。
入口：检索调试页「RAG 评测」标签，或 `/api/v1/evaluations`。
评测第一版（`evaluation-v1.md`）和第二版规划（`evaluation-v2-plan.md`）的内容已合并到本页并删除原文；
历史实测 JSON 保留在 `evals/results/`。

## 目标与边界

第三版用于同页完成案例标注、检索/回答执行、指标统计、证据追踪和人工审核，
并在小样本上比较实验参数。它不推断生产准确率或统计显著性，也不把自动指标当作业务签收结论。
未核验业务案例、草稿案例集和项目文档样例只能用于开发回归。

## 实验输入

- `kind`：`retrieval` 只执行检索；`rag` 走完整知识库问答，会产生模型调用和费用。
- `mode`：`keyword` / `vector` / `hybrid`，与独立检索接口一致。
- `k`：1–20，指标只统计前 K 个文件。
- `file_ids`：可选检索范围；省略为全库，空数组表示不搜索任何文件。
- `options.rerank`：`inherit` 跟随服务配置，`on` 强制启用，`off` 关闭。
- `options.context_tokens`：仅完整 RAG 可设置，用于固定上下文预算实验。
- `idempotency_key`：同一键重复提交相同配置返回原实验，配置不同返回 409。
- 单次最多 20 题，串行执行；同一时间只允许一个评测任务运行。

创建时对资料、模型配置、检索版本和代码生成快照指纹。执行每题前后都会复核指纹，
资料或配置变化时该题不记为成功，避免把不同语料的结果混在同一实验里。

## 案例与标注

每个案例包含：

- `expected`：`answerable` / `unanswerable` / `unlabelled`。
- `judgment_scope`：`full` 表示该题的正负文件已完整标注；`partial` 表示只确认了部分文件。
- `relevant_file_ids`：有答案题的正相关文件；应拒答题不应填写。
- `negative_file_ids`：可选的负相关文件，不能与正相关文件重复。
- `evidence_groups`：有答案题的必要事实，每组 1–5 个原文锚点（文件、内容哈希、引文、页码）。
- `tags`：业务标签，可用于筛选和报告。

有答案标注必须选择相关文件，未标注和应拒答案例不填写相关文件；必要证据必须落在正相关文件内。
部分标注可以记录已知证据，但**部分标注不能证明资料中不存在答案**，因此不参与完整检索质量均值。

## 检索指标口径

五项指标均以**文件**为单位，先按首次出现顺序去重，再取前 K 个：

- `precision@K`：命中文件数 / K。**文件去重、固定 K 分母、完整标注失败题计零**，
  不用实际返回数量做分母。
- `recall@K`：命中文件数 / 标注正相关文件数。
- `hit@K`：前 K 个文件中是否至少命中一个正相关文件。
- `mrr@K`：第一个命中文件的排名倒数，未命中为 0。
- `ndcg@K`：按二值相关性计算 DCG / IDCG。

汇总时**完整正相关案例才参与五项质量均值**：只统计 `expected=answerable` 且
`judgment_scope=full` 的题；已完成和失败题都进入分母，失败题按零分计；
`partial`、`unlabelled` 和未执行的题不计入均值。报告会同时给出计分题数、失败数和
未标注题数，避免用小样本均值暗示总体准确率。

文件级指标只说明资料是否被召回。**文件级命中分开**于回答质量：命中相关文件不代表
答案事实正确、引用充分或该拒答的题已经拒答。回答质量必须另看人工审核和证据追踪。

## 证据锚点与原位追踪

`evidence_groups` 使用原文锚点而不是文件 ID 推断证据。锚点包含文件、内容哈希和引文，
校验内容哈希后，在解析切片、召回候选、重排结果、选中来源和最终上下文中逐级做
规范化子串匹配。原文包含检查**不是语义事实验证**，只回答“这段原文是否到达该阶段”，
不能证明模型理解或使用了它。

每个锚点会被标记为 `matched`、`unmapped` 或 `stale`（文件内容已变化）。
任一必要事实的锚点无法定位时，视为标注缺口，**整题证据覆盖不评分**，
也不把缺口归因给解析或检索。

可评分时按阶段汇总覆盖率，并给出首个失败阶段的归因：

- `retrieval_miss`：召回阶段未包含原文。
- `rerank_loss`：召回包含但重排后丢失。
- `selection_loss`：重排后未进入选中来源。
- `assembly_loss`：选中但未进入最终上下文。
- `annotation_gap`：锚点本身无效或无法定位。

## 人工审核

每道完成的回答可分别记录总评和五个维度：

- `fact_coverage`：事实覆盖 full / partial / none。
- `unsupported_claims`：是否存在无资料支持的表述 yes / no。
- `conditions`：条件、例外和前提是否正确 pass / fail / na。
- `citation_support`：引用对正文的支持 full / partial / none / na。
- `refusal`：应拒答题是否正确拒答 correct / incorrect / na。
- `causes`：失败归因，可多选。

总评支持 pending / pass / partial / fail 和备注。`pending` 与 `na` 不计入通过率分母；
审核未完成时通过率显示为“未评分”，不把待审核当成通过。

## 案例集、冻结与复用

- 案例集按“知识库 + 名称”保存版本，内容、语料、拆分、审核状态或冻结状态变化时新增版本；
  完全相同则复用上一版。
- 案例集绑定保存时的语料指纹（文件 ID + 内容哈希）。资料变化后执行会返回
  `case_set_stale`，需重新核对并保存新版本。
- `split` 区分 `development` 与 `acceptance`，同一批题目不要跨拆分复制近似重复题。
- 冻结要求 `review_status=verified`、全部案例完整标注，且每道有答案题都有能定位的必要原文；
  冻结失败会返回具体错误，不会静默通过。
- 评测可绑定案例集；请求中的案例内容必须与所选版本一致，否则返回 409。

## 快照与可复现

每次实验保存：文件列表与内容哈希、去除密钥后的配置、向量版本、全文版本、
应用代码哈希、实验参数和案例集元数据，并计算统一指纹。快照只保存服务地址的哈希，
不保存 API Key。重试必须沿用原案例、配置和指纹；资料、配置或代码变化时只能新建实验。

## 重试、对比与归档

- `GET /evaluations/{id}/retry-plan` 返回需重试的题目下标、可复用成功题数量和
  结果未知的题目。重试时成功题直接复用（标记 `reused_from`），不重复计费。
- 完整 RAG 中若失败题可能已产生模型调用，重试必须显式 `confirm_unknown_calls`，
  避免在未知结果上再次计费。
- `GET /evaluations/compare` 对比两次实验的快照、案例集、逐题指标和回答状态，
  只有输入一致时才做差值比较。
- 历史支持按知识库、归档状态分页；运行中的实验不能归档或删除。
- 删除实验会清理其专用会话和检查点，结果不可恢复。
- `GET /evaluations/{id}/export` 导出 JSON，`GET /evaluations/{id}/report` 生成 Markdown 报告。

## 操作与复现

界面流程：选择知识库 → 勾选文件与模式 → 添加或载入案例 → 标注正/负相关与必要证据
→ 选择仅检索或完整 RAG → 执行 → 查看指标、证据流转和逐题结果 → 人工审核 → 导出报告。

命令行辅助：

```bash
# 由项目文档生成 30 道待核验草稿（含原文哈希校验）
uv run python -m scripts.prepare_evaluation_v3 --output evals/cases/project-v3-draft.json

# 显式上传资料并保存草稿案例集；--index 会调用 Embedding
uv run python -m scripts.prepare_evaluation_v3 --import-url http://127.0.0.1:8000 --kb-id <kb_id>

# 真实链路小样本冒烟，验证评测、重试、对比和报告
uv run python -m scripts.smoke_evaluation_v3_live
```

## 方法参考

指标与数据组织参考以下公开工作，均为**方法借鉴**，不复现官方数据集或评分流程，也不与公开排行榜比较：

| 来源 | 借鉴内容 |
|---|---|
| [BEIR](https://github.com/beir-cellar/beir/blob/main/beir/retrieval/evaluation.py) | corpus / query / qrels 分离与 IR 排名评估；这里采用二值文件级 qrels 与本地小样本 |
| [MTEB](https://github.com/embeddings-benchmark/mteb) | 任务、配置、数据集版本与结果可追溯的组织方式；只覆盖文本检索与 RAG |
| [RAGBench](https://arxiv.org/abs/2407.11005) | 检索相关性与回答质量**分层评估**，不把文件命中率称作 faithfulness |
| [KILT](https://github.com/facebookresearch/KILT#kilt-data-format) | 答案与 provenance 一起保存；这里落为 `evidence_groups` 原文锚点 |

第三版已取代评测第一版和评测第二版规划，两者文档已删除；本页是评测口径的唯一现行说明。
历史实测 JSON 仍保留在 `evals/results/`，可作为当时版本的证据，但其中数值不代表当前口径。

## 已知边界

- 指标是文件级二值相关性，不评价段落排序质量。
- 证据追踪依赖原文精确包含和内容哈希；改写、同义表达和跨切片拼接不会被识别为证据。
- 小样本均值、优先级和显著性都不足以支撑生产结论；需要真实业务标注和更大样本。
- 回答正确性仍以人工审核为准，自动指标不能替代事实核验。
