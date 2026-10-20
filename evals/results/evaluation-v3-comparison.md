# RAG 同题实验比较

基线 A：6a7b089e-e1bb-4da5-833a-57a7ed783a75；对照 B：142c88e4-c330-4bf2-9760-959ddcb21e01
胜平负：{"win":0,"tie":1,"loss":0,"unscored":1}
参数差异：{"mode":{"left":"keyword","right":"keyword"},"options":{"left":{"rerank":"off","context_tokens":512},"right":{"rerank":"off","context_tokens":1024}}}

| 问题 | 结果 | Δ Recall | Δ nDCG | Δ ms | Δ Token |
|---|---|---|---|---|---|
| 员工每年有多少天带薪年假，谁审批？ | 持平 | 0.0 pp | 0.0 pp | -351.2 ms | +6.0 tokens |
| 员工生日礼金的精确金额是多少？ | 未评分 | — | — | +17.4 ms | 0.0 tokens |

小样本仅作诊断，不推断显著性。复用结果不参与真实执行用量/耗时比较。

配置差异：{}