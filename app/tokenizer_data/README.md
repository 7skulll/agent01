# 离线 BPE 数据

编码表来自 OpenAI 官方发布地址：

- https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
- https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken

SHA-256 固定在 `app/tokenization.py`，缺失或不匹配即报错，不运行时下载。
正则表达式参考 tiktoken 0.14.0 的 `tiktoken_ext/openai_public.py`；许可证见
`LICENSE.tiktoken`，上游：https://github.com/openai/tiktoken。

两者用于其各自词表的精确文本编码；其他模型与聊天模板的 token 用量仍为估算。
部署时应核对供应商限制和实际 usage。编码表、正则或切片语义变更需要升级切片版本。
