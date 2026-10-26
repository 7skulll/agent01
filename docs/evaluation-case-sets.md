# RAG 测评集清单与整理结果

更新：2026-10-26。数据来自当前业务库的 `evaluation_case_sets`，已完成归档、新增验收集和知识库归档。

备份与审计：

- [case-sets.json](../evals/case-sets/2026-10-26/case-sets.json)：12 条记录的完整案例、语料、指纹、引用数和 `archived` 状态。
- [summary.csv](../evals/case-sets/2026-10-26/summary.csv)：一行一套的汇总表。

重新导出：

```bash
uv run python -m scripts.export_evaluation_case_sets
```

## 当前结论

- **12 条测评集记录**，分布在 **3 个知识库**。
- 按名称去重后 **8 个测评集**，共 **173 条案例记录**。
- 其中 **5 条已归档**：`CRUD-RAG-reviewed-v1` 的 4 条重复副本，以及被 `CRUD-Acceptance-v1` 取代的旧 CRUD acceptance 批。
- 新增两套验收集：`Synthetic-Acceptance-v1` 和 `CRUD-Acceptance-v1`，均为 `verified` + `frozen`。
- `CRUD-RAG-reviewed-v1` 知识库本身也已归档，默认不出现在知识库选择器。

## 主从关系

| 用途 | 测评集 | 状态 |
|---|---|---|
| 开发检索基线 | `Synthetic-Retrieval-Baseline-20` | 20 题，full，draft |
| RAGAS 冒烟 | `RAGAS-smoke-e429` | 1 题，full，draft |
| 合成小样本验收 | `Synthetic-Acceptance-v1` | 15 题，verified + frozen |
| CRUD 开发批 | `CRUD-RAG-60-v1` 的 3 个 development 批 | 42 题，partial，draft |
| CRUD 验收集 | `CRUD-Acceptance-v1` | 18 题，full，verified + frozen |
| CRUD 历史副本 | `CRUD-RAG-reviewed-v1` 四批 | 59 题，**已归档** |
| 旧 CRUD 验收批 | `CRUD-22-b199fb28-acceptance-1`（60-v1 内） | 18 题，**已归档** |

## 新增验收集

### `Synthetic-Acceptance-v1`

- 10 道有答案题，覆盖 E429、E401、ZX-314、2026 采购审批、外包年假、定制退款、数据库备份、加班餐费、增值税发票、入职设备。
- 5 道应拒答题：子女教育补贴、ZX-999 防水等级、2024 营业收入、远程办公额度、食堂午餐时间。
- 10 道有答案题全部带 `evidence_groups`。
- `split=acceptance`、`review_status=verified`、`frozen=true`。

### `CRUD-Acceptance-v1`

- 来源：原 `CRUD-22-b199fb28-acceptance-1` 的 18 题。
- 标注范围从 `partial` 改为 `full`。
- 18 道题全部补了必要证据，引文与文件内容哈希已程序化核对。
- `split=acceptance`、`review_status=verified`、`frozen=true`。
- 旧 acceptance 批已归档，避免两个验收版本混淆。

**说明**：两套验收集的 `verified` 是程序化核验（引文能在对应文件切片中精确匹配、文件哈希一致），不是业务同事的人工签收。正式业务验收前仍建议人工复核。

## 实测结果

### `Synthetic-Acceptance-v1` · 仅检索

- 15 题全部完成，0 失败。
- precision@5 **20.0%**，recall@5 **100%**，hit@5 **100%**，MRR@5 **100%**，nDCG@5 **100%**。
- 10 道有答案题证据覆盖全部有效。

### `Synthetic-Acceptance-v1` · 完整 RAG

- 15 题全部完成，0 失败。
- 10 道有答案题全部 answered。
- 5 道应拒答题全部 `insufficient_evidence`，拒答成功 **5/5**。
- 无错误拒答。

### `CRUD-Acceptance-v1` · 仅检索

- 18 题全部完成，0 失败。
- precision@5 **38.9%**，recall@5 **98.1%**，hit@5 **100%**，MRR@5 **97.2%**，nDCG@5 **97.0%**。
- 18 道题证据覆盖全部有效。
- 有 1 道题存在相关文件未全部进入前 K 的召回损失，适合作为后续检索优化对象。

### `CRUD-Acceptance-v1` · 完整 RAG

- 18 题中 15 完成、3 失败，失败码均为 `model_response_truncated`。
- 失败题按零分计入 recall，导致汇总 recall 降到 **83.3%**；这不是检索漏召回，而是回答输出达到 `RAG_LLM_MAX_TOKENS=2048` 的上限。
- 3 道失败题的检索与证据覆盖本身正常（都有 10 个来源，evidence valid=true）。
- 建议把回答输出预算提高到 4096 或更高，或缩短长答案；当前配置下长 CRUD 答案会稳定触发截断。

## 归档与只读

- `CRUD-RAG-reviewed-v1` 知识库已归档：默认不出现在选择器；新检索、问答、评测会被 `require_knowledge_base` 拒绝；历史评测仍可查看。
- `CRUD-RAG-reviewed-v1` 的四条测评集已归档，页面上勾选「显示已归档」可以查看或取消归档。
- 旧的 CRUD acceptance 批已归档，历史引用为 0。

## 当前缺口

- CRUD 的 3 个 development 批仍是 `partial`，这是开发用途，不参与严格五项指标均值；正式验收请用 `CRUD-Acceptance-v1`。
- 两套验收集的机器核验不能替代业务人工审核。
- CRUD 验收集只覆盖 18 道原始 acceptance 题，尚未覆盖完整 60 题。
- 长答案会触发 `RAG_LLM_MAX_TOKENS=2048` 截断；验收前应确认输出预算足够。

## 相关操作

```bash
# 导出/审计测评集
uv run python -m scripts.export_evaluation_case_sets

# 归档重复测评集
uv run python -m scripts.archive_duplicate_case_sets --apply

# 归档重复知识库
uv run python -m scripts.archive_duplicate_knowledge_base --apply
```

页面上的「显示已归档」只影响测评集列表；已归档知识库暂时通过 API/脚本取消归档：

```bash
curl -X PATCH "$RAG_API_BASE/knowledge-bases/<kb_id>/archive" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"archived": false}'
```
