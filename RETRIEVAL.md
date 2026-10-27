# 检索行为与调优

## 检索模式

默认使用混合检索：向量召回与 BM25 全文召回各返回一批候选，再做两级 RRF 融合。关键词检索与向量检索可单独选择：前者不依赖 Embedding，后者仅使用语义相似度。向量召回固定使用 pgvector 精确余弦，不构建 HNSW 等近似索引，也不受 `hnsw.*` 参数影响。

## 排名与过滤契约

结果按最终 `rank` 升序返回。近重复软降权可能调整次序，下游应使用 `rank`，而非重新按 `score` 排序。

`score_type` 标明分数含义：`vector_similarity`、`bm25`、`character_overlap`、`recall_rrf`、`rerank_model` 或 `rerank_rrf`。`rank_source` 表示是否经过重排。重排候选之外的尾部保留召回分数和类型，始终排在已重排候选之后；不要跨类型比较阈值。`diagnostics.scores_comparable` 表明当前结果是否只有一种分数类型。

`min_score` 仅在纯向量模式使用。混合和关键词模式在 `diagnostics.unapplied_parameters` 显式返回未应用原因，检索调试页同步展示。重排原始分数阈值仍由 `retrieval_rerank_min_score` 单独控制，默认不设置；需要业务样本校准，字符回退也不代表足以回答。

## 全文检索

多条查询在一次 BM25 SQL 中执行，各自计算分数、Top-K，再执行原有的路内 RRF。词项查找使用既有 `(term, chunk_id)` 主键。文档数、Token 总数随每个文件的全文索引原子更新，查询只聚合有效文件的统计，不再物化全库切片和 Token 数组。文件范围只限制返回候选，不改变知识库 IDF 和平均长度。

Jieba 无命中且存在有效查询词时，使用独立的字符词项 GIN 索引回退：汉字双字词项、完整 ASCII 标识。至少匹配一半不同词项；返回值以 `character_fallback` 标识。它仅用于补充零命中查询，不保证修复已有错误命中的分析器问题。所有路径保留知识库及文件范围过滤。

迁移 `012_retrieval_search.sql` 回填文件统计，并为已有切片生成字符词项。已有全文索引无需因本次修改重建。首次迁移会回填现有数据并创建索引，大库应预留维护时间。它不增加重复的 posting 词项索引。匹配高频词时仍可能读取大量 posting，性能需要按实际语料验证。

## 去重与上下文

正文按连续空白归一化判定重复，但返回原文并保留其他来源。代码、表格和列表保留内部空白语义。近重复使用字符片段重合度软降权，不删除可能带有不同数值或约束的证据，也不会让未重排尾部越过已重排候选。

上下文装配在每次请求内缓存 Token 计数和片段快照，完整 JSON 和消息预算仍精确复检，不依赖片段 Token 的简单相加。超预算表格可选择完整相关行，保留可识别表头与说明，并记录原文范围和截取标志；最小安全单元仍放不下时明确记录跳过原因。代码、列表继续保留整块，避免裁剪破坏语义。


## 重排独立审计

`scripts/audit_rerank.py` 用固定合成候选检查重排能力，不执行实际召回或回答生成。
默认读取 `evals/retrieval_baseline.json` 与 `evals/bm25_holdout.json`，分别构造候选，
以固定种子的随机初排对比配置权重、0.75 RRF 和纯模型排序。同一题复用一批真实模型分数，
不为比较策略重复调用模型。运行会访问配置的重排服务。

```bash
uv run python -m scripts.audit_rerank --enable
uv run python -m scripts.audit_rerank --enable --dataset evals/bm25_holdout.json --output evals/results/rerank-holdout.json
```

`--dataset` 可重复指定。`--order relevant-last` 是使用标签把相关文件置底的显式压力场景，
应与默认随机初排分开解释。审计关闭相关性阈值，不修改生产配置；报告保存候选覆盖率、
语料哈希、模型分数和逐题排名。默认历史报告见
[rerank-audit-v2.json](evals/results/rerank-audit-v2.json)。
历史合成集的高分不能代表端到端 RAG 准确率；被反复观察的 holdout 不再属于盲测集。
