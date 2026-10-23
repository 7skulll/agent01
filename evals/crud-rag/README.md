# CRUD-RAG 测试数据接入

本项目复用 CRUD-RAG 官方新闻及参考答案，运行自己的 RAG 实现。无需安装官方项目的旧版
依赖、Milvus 或本地 BGE 模型；继续使用本项目的 PostgreSQL、检索和模型配置。

## 已准备的第一版

- 官方仓库：[IAAR-Shanghai/CRUD_RAG](https://github.com/IAAR-Shanghai/CRUD_RAG)
- 固定提交：`1aace383994e1f68efa12cf2a8e2dadfb4102ceb`
- 数据：`data/crud_split/split_merged.json`，下载大小 26,866,460 字节。
- SHA-256：`b199fb281ac526c1c8da7364cad7ea22b7f6fe1d4167e511edbdda8ad3bd943a`
- 从 `questanswer_1doc`、`questanswer_2docs`、`questanswer_3docs` 各抽 20 题，共 60 题。
- 随机种子 22；开发 42 题、预留验收 18 题。相同事件、相同新闻或相同问题先关联分组，
  再分集合，避免同一事件的不同文档题进入不同集合。未进行语义级近重复的人工核验。
- 配套新闻按原文去重为 120 篇；另加 100 篇其他事件新闻，共 220 篇检索资料。
- 参考答案在 manifest 中，上传目录仅包含原始新闻，不上传问题、答案、event 或 thoughts。

上游原始问答共 2,394 条（800 / 797 / 797）。本地子集使用问答自带的 news 字段和
event_summary 中的原始新闻，不是官方八万余篇全语料实验，成绩不与论文排行榜直接比较。
八万余篇完整语料的接入是后续扩展，不是当前命令的行为。

这是历史新闻测试集，问题里的时间属于原文背景，不代表当前事实。上游 README 展示
Apache-2.0 标识；保留仓库、提交、原始新闻出处及论文引用信息，数据暂存本地被忽略的
`data/` 目录。新闻本身含第三方来源，仓库代码标识不等于所有新闻的独立授权说明。

## 一、重新下载和制作

在项目根目录执行，已有本地数据时可以跳过下载：

```bash
uv run python -m scripts.prepare_crud_rag download
uv run python -m scripts.prepare_crud_rag prepare
uv run python -m scripts.prepare_crud_rag validate
```

制作命令完全离线，不连接数据库、不调用模型。输出目录必须不存在，避免覆盖审核记录。
若第一版已存在，想试其他抽样规模，请换目录：

```bash
uv run python -m scripts.prepare_crud_rag prepare --per-task 40 --distractors 200 --output data/crud-rag/subset-v2
```

输出：

```text
data/crud-rag/upstream.json             固定版本的官方原始文件
data/crud-rag/subset-v1/manifest.json    问题、参考答案、配套文档 ID、任务和分组
data/crud-rag/subset-v1/documents/      只含原始新闻，实际上传到知识库
data/crud-rag/subset-v1/SUMMARY.md      抽样数量和使用边界
```

`data/` 已在项目 `.gitignore` 中；换机器时重新下载并执行相同命令即可复现。

## 二、导入工作台

1. 启动本项目后端和前端，确认 PostgreSQL 可用。
2. 在页面新建专用知识库，例如 `CRUD-RAG-60-v1`；复制知识库 ID。
3. 执行下面命令，将 `你的知识库ID` 替换为实际值：

```bash
uv run python -m scripts.prepare_crud_rag import --kb-id 你的知识库ID
```

默认后端地址是 `http://127.0.0.1:8000`，可以用 `--url` 修改。鉴权读取根目录 `.env` 的
`RAG_API_TOKEN`（管理员服务令牌）；也可通过环境变量 `CRUD_RAG_API_TOKEN` 覆盖，不在命令行或报告中打印密钥。

导入器拒绝 `default` 库及含其他资料的库。上传使用文档哈希作幂等键，可重试；不删除文件。
所有文档先上传，再保存案例集，避免后续上传改变已保存案例的语料版本。
开发题分为 20/20/2 三批，验收题 18 题一批，共四个案例集，兼容现有每批 20 题的限制。

导入后本地还会产生：

- `binding-*.json`：该服务器、知识库对应的文档绑定记录。
- `case-sets-bound.json`：案例集保存结果。
- `cases-development-1.json` 等：每批可在同一知识库页面导入的案例 JSON。
- `cases-bound.json`：全部 60 题的绑定结果，仅用于归档；不要直接导入有 20 题限制的页面。

这些文件绑定具体的服务器和知识库，跨环境时重新执行导入，不复用 file_id。

## 三、建立索引并运行

在文件页点击构建索引，等待全文状态可用。也可以显式使用：

```bash
uv run python -m scripts.prepare_crud_rag import --kb-id 你的知识库ID --index
```

`--index` 会触发实际解析与索引；若配置 Embedding，可能发生模型调用和费用。
导入默认不建索引、不运行评测。首次建议先在开发集选择关键词检索，再比较向量/混合方案。

在“检索调试 → RAG 评测”载入开发案例集，选择检索或完整 RAG 后运行。
完整 RAG 会调用当前配置的对话模型；参考答案放在“必要事实”栏供人工对照，并不作为
真实问答请求的模型输入。当前没有自动语义裁判，不会自动给回答正确率。

## 四、先审核，再谈召回率和验收

**全部导入为 draft、partial，未冻结。** news1/news2/news3 是官方配套新闻，不能直接
视为全库中所有相关文档；两文档/三文档任务也不保证每篇都不可替代。
100 篇干扰候选不自动标成负相关，无答案题不自动伪造。

因此刚导入时，完整 Recall/Precision 等显示未评分是预期行为。可先看已标注文档命中、
已判定候选覆盖及逐题回答。不要为了显示指标而批量切换到 full。

每题审核步骤：

1. 阅读问题、上游参考答案及配套新闻，检查是否真的有答案、是否有时间或事实歧义。
2. 将参考答案拆成独立必要事实，给每个事实填写短原文证据，可添加等价出处。
3. 汇集关键词、向量、混合的候选，补充相关资料并核对重复新闻；未知资料保留未知。
4. 只有对本次固定语料完成相关性核验，才改为 full；否则继续 partial。
5. 修正错误后另存版本。验收题完成核验后再标记 verified 并冻结。

原文证据目前至少 4 字、最长 2,000 字，不支持跨切片长句匹配。应选能支持事实的完整短句，
不要把整篇新闻或仅包含答案词的无关句当作证据。

最初的 18 题只是预留验收候选，并不是已核验金标。评测制作过程中已浏览过的题不应再
宣称严格盲测；正式签收可从未使用的事件组另建并封存一批。

## 后续顺序

先审核 10–20 道开发题，确认现有界面、证据和指标口径可用；再完成 60 题审核。
需要测更真实的检索难度时，扩大到官方全语料，重新审核相关性及无答案标签。
最后增加自动回答裁判和批量实验汇总，避免先放大题量再发现标注口径有误。

## 2026-10-22 实测复核

复核后的报告见 [实测记录与修订](subset-v1-run-2026-10-22.md)。争议题的排除原因保存在
`review-overrides-v1.json`，另存 `data/crud-rag/subset-v1-reviewed/`，原始版本和历史结果不变。

```bash
uv run python -m scripts.prepare_crud_rag revise
uv run python -m scripts.prepare_crud_rag validate --folder data/crud-rag/subset-v1-reviewed
```

已保存结果的离线重算：

```bash
uv run python -m scripts.audit_crud_results --folder data/crud-rag/runs/2026-10-22-original --output evals/crud-rag/subset-v1-audit-2026-10-22.json
```

`revise` 默认另存路径且拒绝覆盖；本机已生成，直接执行 validate 即可。
修订数据为59题（开发41、预留验收18），其他题仍未人工核验，不等于正式验收金标。
没有自动修改工作台既有案例集；需要导入修订版时显式指定新目录，按上面的独立知识库流程操作。
新的后端代码让检索与问答按相同候选片段预算取数，输出中保留请求片段上限与文件排名单位；
代码需要由新的后端进程加载，不会回写历史实验结果。
