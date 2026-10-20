# RAG 同题实验比较

基线 A：6d442b92-170b-4f76-977d-5e054f84ce34；对照 B：b408f354-8474-4914-81ba-dab20fda9f6c
胜平负：{"win":0,"tie":1,"loss":0,"unscored":1}
参数差异：{"mode":{"left":"hybrid","right":"hybrid"},"options":{"left":{"rerank":"on","context_tokens":512},"right":{"rerank":"on","context_tokens":1024}}}

| 问题 | 结果 | Δ Recall | Δ nDCG | Δ ms | Δ Token |
|---|---|---|---|---|---|
| 员工每年有多少天带薪年假，谁审批？ | 持平 | 0.0 pp | 0.0 pp | +6.2 ms | +5.0 tokens |
| 员工生日礼金的精确金额是多少？ | 未评分 | — | — | +114.3 ms | 0.0 tokens |

小样本仅作诊断，不推断显著性。复用结果不参与真实执行用量/耗时比较。

配置差异：{}