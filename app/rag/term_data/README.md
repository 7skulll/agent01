# FastGPT 分词资源来源

stopwords.json 从本地 FastGPT-main/packages/service/common/string/jieba/index.ts 的
stopWords 集合提取，保留其中的字符串成员；JS 嵌套数组成员无法命中字符串 token，因此未纳入。
源码的清洗顺序移植于 app/rag/terms.py。FastGPT 许可保存在 FASTGPT-LICENSE。

分词使用 rjieba 0.1.13（jieba-rs Python binding，MIT 许可），HMM=true，使用包内置词典，
不增加其他词项或用户词典。其实际分词结果已与 FastGPT @node-rs/jieba@2.0.1 加载本地
FastGPT dict.json 对照：90 条样例完全一致；词典的 349,045 个词逐词分词摘要一致。
来源哈希及对照摘要位于 tests/fixtures/fastgpt_terms.json。

FastGPT: https://github.com/labring/FastGPT
rjieba: https://github.com/messense/rjieba-py
