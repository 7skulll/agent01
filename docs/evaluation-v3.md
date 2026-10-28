# 同页 RAG 评测第三版

更新：2026-10-27。本文说明当前评测流程；计算规则见 [指标口径](evaluation-metrics.md)。
入口：检索调试页「RAG 评测」标签，或 `/api/v1/evaluations`。

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
- `options.judge`：API 省略时为 `none`；页面快捷评测在 worker 可用时默认选择完整 RAG 与 `ragas` 自动评分。需安装独立 worker，会增加模型调用；选择 `none` 时新回答需要另行人工审核。
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
- `required_facts`：供审核对照的必要事实说明。
- `reference_answer`：可选参考答案，用于人工对照和 RAGAS 参考评分，不进入问答模型输入。
- `tags`：业务标签，可用于筛选和报告。

有答案标注必须选择相关文件，未标注和应拒答案例不填写相关文件；必要证据必须落在正相关文件内。
部分标注可以记录已知证据，但**部分标注不能证明资料中不存在答案**，因此不参与完整检索质量均值。

## 检索指标口径

五项指标均以**文件**为单位，先按首次出现顺序去重，再取前 K 个：

- `precision@K`：命中文件数 / K。**文件去重、固定 K 分母**，
  不用实际返回数量做分母。
- `recall@K`：命中文件数 / 标注正相关文件数。
- `hit@K`：前 K 个文件中是否至少命中一个正相关文件。
- `mrr@K`：第一个命中文件的排名倒数，未命中为 0。
- `ndcg@K`：按二值相关性计算 DCG / IDCG。

汇总时**完整正相关案例才参与五项质量均值**：只统计 `expected=answerable` 且
`judgment_scope=full` 的题；已完成和失败题都进入分母。回答生成失败但保留了检索结果的题按实际检索计分；无可用检索结果的失败题计零；
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

每道完成的回答可分别记录总评和六个维度：

- `fact_coverage`：事实覆盖 full / partial / none。
- `unsupported_claims`：是否存在无资料支持的表述 yes / no。
- `answer_relevancy`：回答是否回应问题 relevant / partial / irrelevant。
- `conditions`：条件、例外和前提是否正确 pass / fail / na。
- `citation_support`：引用对正文的支持 full / partial / none / na。
- `refusal`：应拒答题是否正确拒答 correct / incorrect / na。
- `causes`：失败归因，可多选。

总评支持 pending / pass / partial / fail 和备注。`pending` 与 `na` 不计入通过率分母；
审核未完成时通过率显示为“未评分”，不把待审核当成通过。早于某个维度存在的旧记录没有该字段，
读取时按 `pending` 计入待审核数，不计入分母。

### 审核来源与继承

审核是逐条结果保存的，因此新运行不会自动继承旧运行的判定。审核带下列溯源字段，
页面在评价表单上方展示，报告导出保留：

| 字段 | 含义 |
|---|---|
| `reviewer` | 保存者显示名；工作台保存时取当前登录员工，脚本写入时取文件声明 |
| `reviewed_at` | 保存时间（ISO 8601） |
| `review_method` | `human` 登录员工在工作台保存 / `ai_proxy` AI 代审 / `carried` 沿用旧运行 |
| `carried_from` | 继承来源，格式 `评测ID:题号`；人工重新保存会清空该字段 |

工作台保存时 `review_method` 固定为 `human` 并记录登录账号，代审与人工签收在数据上可区分。
`scripts/carry_reviews.py` 只在**同案例集、同问题、回答文本完全一致**时把旧判定复制到新运行，
并写入 `carried_from`；回答有任何变化时只列出待复审题，不复制判定。
`scripts/import_reviews.py` 按答案哈希校验后写入外部审核表，拒绝哈希不符、无回答或已有记录的行。

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
  要求同一知识库、运行类型、K、案例集版本、案例标注与范围内语料一致，且实验完整结束；
  检索模式、重排与上下文参数可作为实验变量。失败题计分差异见 [当前对比限制](evaluation-metrics.md#当前实验对比限制)。
- 历史支持按知识库、归档状态分页；运行中的实验不能归档或删除。
- 删除实验会清理其专用会话和检查点，结果不可恢复。
- `GET /evaluations/{id}/export` 导出 JSON，`GET /evaluations/{id}/report` 生成 Markdown 报告。

## 回归门禁

`GET /evaluations/gate?baseline=<id>&candidate=<id>` 把两次实验的配对差值转成可判定的检查项，
判定规则与 [指标口径](evaluation-metrics.md) 一致：

| 检查项 | 来源 | 默认允许下降 | 查询参数 |
|---|---|---:|---|
| 召回率 | 检索层 | 2 个百分点 | `max_recall_drop=0.02` |
| MRR | 检索层 | 2 个百分点 | `max_mrr_drop=0.02` |
| 忠实度、答案相关性、正确性、完整性 | 生成层 | 5 个百分点 | `max_answer_drop=0.05` |

每项按 `delta >= -max_drop` 判 `pass`，否则 `fail`；该项无可比数据时为 `unscored`。
整体状态取最差：任一 `fail` 即 `fail`，否则任一 `unscored` 即 `unscored`，全部通过才 `pass`。
**`unscored` 不等于通过**，报告会列出每项的来源、差值和阈值。

门禁复用 `compare()` 的兼容性校验，知识库、运行方式、K、案例集版本、语料或标注不一致时
直接返回 409，不做跨实验推断。仓库没有 CI 配置，门禁目前只能在页面或手动调用接口触发。
失败题的口径差异见 [当前对比限制](evaluation-metrics.md#当前实验对比限制)。

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

历史实测 JSON 保留在 `evals/results/`，可作为当时版本的证据，其中数值不代表当前口径。
`evals/results/` 只保留原始 JSON 证据；由它渲染的 Markdown 报告已删除，需要时从 JSON 重新生成。

## 已知边界

- 指标是文件级二值相关性，不评价段落排序质量。
- 证据追踪依赖原文精确包含和内容哈希；改写、同义表达和跨切片拼接不会被识别为证据。
- 小样本均值、优先级和显著性都不足以支撑生产结论；需要真实业务标注和更大样本。
- 回答正确性仍以人工审核为准，自动指标不能替代事实核验。
