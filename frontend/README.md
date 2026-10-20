# 知序 · RAG 工作台

Vue 3 + TypeScript 前端，连接仓库现有 FastAPI 后端。包括问答、知识库文件和检索调试三个页面。

## 开发启动

需要 Node.js 22.12+（或 20.19+）及 npm。后端需要 PostgreSQL/pgvector，以及根目录 `.env` 配置。

在项目根目录启动后端：

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在另一个终端进入 `frontend/`：

```bash
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api` 请求代理到 `http://127.0.0.1:8000`。修改后端地址可以执行 `API_PROXY_TARGET=http://127.0.0.1:其他端口 npm run dev`。

前端默认同源访问 API；独立域名部署时可以复制 `.env.example` 为 `.env.local` 并设置 `VITE_API_BASE`，同时设置后端 CORS。所有 `VITE_` 变量都对浏览器可见，不能放入模型密钥。

如果后端设置了 `RAG_API_TOKEN`，前端需在 `.env.local` 设置相同的 `VITE_API_TOKEN`，所有请求会携带 `Authorization: Bearer`。这是单工作区部署门禁，不是用户级权限模型。

## 演示流程

1. 在右上角选择知识库，或点击“+”新建。可删除不用的库，删除会清理原文件与索引并保留历史问答；有未结束任务时需先完成或取消。
2. 知识库文件：上传 TXT、MD、PDF、DOCX 或 XLSX，点击“构建 / 重试索引”。Word/Excel 旧格式需先转换，扫描 PDF 暂不支持 OCR。
3. 等待文件全文状态变为“可用”。向量状态与全文独立；没有 Embedding 时仍可用全文检索。
4. 检索调试：输入问题并选择全文、向量或混合检索，查看片段、分数、耗时和降级原因。
5. 问答工作台：选择检索方式并提问。任务完成后点击引用查看来源；刷新后来源仍可查看。
6. 运行中可以暂停或取消；服务重启后的未完成任务显示等待恢复，由用户手动继续。

问答支持普通对话开关。检索调试跳转问答时仅传递原问题及模式，文件范围、扩展查询和条数不传递。

会话列表支持改名和确认删除；文件行支持下载和确认删除，问答输入框上方支持文件范围。
清理上传残留作用于全库，只处理超过 24 小时且没有记录引用的受管对象。
操作规则与冲突处理见 [日常管理](../docs/daily-management.md)。原文 PDF 预览尚未实现。

## 检查与测试

```bash
npm run build        # vue-tsc 类型检查 + Vite 生产构建
npm test             # SSE 分帧、去重、草稿重置等单测
npm run test:e2e     # 浏览器契约和交互回归，需要本机 Google Chrome
```

浏览器测试使用确定性的 API 响应，覆盖历史来源刷新、检索参数、上传验证、移动/桌面布局、SSE 重连、幂等重试和 Markdown 安全。服务端真实链路另通过根目录 `scripts/smoke_rag.py` 验证，两者的证据分别记录。

后端新增接口的测试：在项目根目录设置独立 `RAG_TEST_DATABASE_URL` 后执行 `uv run pytest tests/test_turns.py -q`。测试会建立并清理专用 schema，不要指向业务数据库。

## API 类型更新

HTTP 类型来自随代码保存的 `openapi.json`。后端协议变更后，在根目录执行以下导出（导出显式使用默认配置，不启动服务或导出模型配置）：

```bash
uv run python -c 'import json; from pathlib import Path; from app.main import create_app; from app.config import Settings; Path("frontend/openapi.json").write_text(json.dumps(create_app(Settings(_env_file=None)).openapi(), ensure_ascii=False, indent=2)+"\n")'
cd frontend
npm run api:generate
```

`Source`、检索 diagnostics 和 SSE 数据在后端目前包含宽松字典，前端在 `src/api/types.ts` 及 `src/features/chat/` 中单独声明并处理。

## 构建与托管

`npm run build` 生成 `dist/`。开发预览使用 `npm run preview`（它不代理 API，需配合公开 API 地址或正式反向代理）。

正式演示可参考 `nginx.conf.example`，将 `dist/` 同源托管并反向代理 `/api`；需要 SPA 路由回退、关闭 SSE 缓冲和足够的读取超时。后端保持单 worker。

## 状态约定

- 服务端任务快照中的 answer/sources 是正式结果；普通对话展示流式草稿，
  rag-answer-v2 知识库回答仅在协议校验完成后发布，不展示原始 JSON 草稿。
- SSE EOF、连接故障和 `stream_error` 不表示任务已失败或完成；重连从已应用的事件序号继续。
- 页面刷新丢失草稿时从事件 0 重放，不使用只保存游标的方式跳过草稿前半段。
- 提交结果不确定时，同样的消息、模式和知识库设置复用本次幂等键；修改内容生成新键。
- 历史轮次来自 `/conversations/{id}/turns`，以 run_id 关联来源，committed 表达是否属于已提交会话链。
- 会话与问答历史统一使用无限分页缓存；刷新会顺序更新所有已加载页面，避免新记录挤出分页边界。
- 上传队列串行执行；其他文件上传期间点击失败项重试，该项仍会在本轮队列中继续处理。


## 界面设计

工作台使用暖白背景、深墨绿导航和低饱和绿色强调，问答、文件管理与检索调试共享字体、
边框、圆角、按钮和状态样式。问答欢迎区提供三类提问入口；桌面使用卡片布局，手机使用纵向列表。
文件页突出上传与索引操作，检索页保留配置与证据结果两栏。页面适配桌面和手机，
系统开启减少动态效果时会关闭装饰性动画。样式入口为 `src/style.css`，主色通过 `--accent` 配置。


### 对话记录与检索选择

问答页顶部的侧栏图标可显示或隐藏对话记录。桌面端记住折叠状态，收起后不丢失当前会话或草稿；
手机端以遮罩侧栏展示，点击空白区域或按 Esc 关闭。

输入区的检索方式采用统一菜单，显示当前选中项及各模式说明；支持方向键、Home/End、
Enter/空格选择、Esc 关闭和 Tab 移出。关闭“使用知识库”时禁用选择器，已选模式保持不变。
菜单向上展开并限制在屏幕内。


## 自定义模型

侧栏“模型设置”提供对话和 Embedding 两张独立表单。保存成功后密码框清空，留空保留服务器
已设置的密钥；保存一张表单不会覆盖另一张表单尚未保存的内容。支持恢复环境配置、显示任务
冲突和索引重建提示。详见 [模型设置](../docs/model-settings.md)。


## 同页评测

检索调试 → RAG 评测：单题/最多 20 题、真实检索或完整问答、文件标注及指标曲线、证据流转与人工审核。
无需新前端依赖。评测结果保存服务端，普通聊天列表不展示评测会话。
第三版支持案例集核验/冻结、独立实验参数、质量总览、标签筛选、分页归档、关联重试和 Markdown 报告。导入先验证嵌套结构，错误保留草稿；实际 Token 缺失显示未知，历史先按当前知识库过滤。
操作、指标口径、真实浏览器验收与后续边界见 [评测第三版](../docs/evaluation-v3.md)。
