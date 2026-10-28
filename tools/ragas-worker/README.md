# RAGAS worker

独立运行的 RAGAS LLM-as-judge。主服务不安装 ragas、pandas、datasets 或 pyarrow，
只通过 JSON stdin/stdout 调用本 worker。

## 安装

```bash
cd tools/ragas-worker
uv sync
```

会在 `tools/ragas-worker/.venv` 创建独立环境。

## 探活

```bash
.venv/bin/python worker.py --probe
```

## 协议

stdin：

```json
{
  "records": [
    {
      "question": "员工年假多少天？",
      "answer": "十天。",
      "contexts": ["员工每年享有十天带薪年假。"],
      "reference": "员工每年享有十天带薪年假。"
    }
  ],
  "llm": {
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "api_key": "...",
    "max_tokens": 1024,
    "temperature": 0
  },
  "embeddings": {
    "model": "qwen3.7-text-embedding-flash",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "..."
  }
}
```

stdout：

```json
{
  "scores": [
    {
      "faithfulness": 1.0,
      "answer_relevancy": 0.9,
      "answer_correctness": 0.8,
      "completeness": 1.0
    }
  ],
  "ragas_version": "0.2.15",
  "worker": "tools/ragas-worker",
  "judge_model": "deepseek-flash",
  "thinking_mode": "disabled",
  "judge_max_tokens": 4096
}
```

没有参考答案时，`answer_correctness` 和 `completeness` 返回 `null`。

## 主服务配置

默认使用：

```text
tools/ragas-worker/.venv/bin/python
tools/ragas-worker/worker.py
```

也可以用环境变量覆盖：

```bash
RAG_RAGAS_WORKER_COMMAND="/opt/ragas/bin/python /opt/ragas/worker.py"
RAG_RAGAS_WORKER_TIMEOUT_SECONDS=300
RAG_RAGAS_DISABLE_THINKING=true
```

主服务只会检查 worker 可执行文件是否存在，并通过 `--probe` 确认 ragas 版本。
使用 DeepSeek 时，RAGAS 默认关闭思考模式以减少判分耗时；这只影响评审模型调用，不改变知识库问答模型的模式。可将 `RAG_RAGAS_DISABLE_THINKING=false` 恢复思考模式。
