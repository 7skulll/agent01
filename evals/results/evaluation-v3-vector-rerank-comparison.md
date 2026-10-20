# RAG 同题实验比较

基线 A：d62aed62-ba5f-4a22-b148-02b61e0d6144；对照 B：67875454-c7e4-4a5e-9f32-5a0935acf40a
胜平负：{"win":0,"tie":1,"loss":0,"unscored":1}
参数差异：{"mode":{"left":"vector","right":"vector"},"options":{"left":{"rerank":"on","context_tokens":512},"right":{"rerank":"on","context_tokens":1024}}}

| 问题 | 结果 | Δ Recall | Δ nDCG | Δ ms | Δ Token |
|---|---|---|---|---|---|
| 员工每年有多少天带薪年假，谁审批？ | 持平 | 0.0 pp | 0.0 pp | -631.9 ms | 0.0 tokens |
| 员工生日礼金的精确金额是多少？ | 未评分 | — | — | +314.0 ms | +9.0 tokens |

小样本仅作诊断，不推断显著性。复用结果不参与真实执行用量/耗时比较。

配置差异：{}