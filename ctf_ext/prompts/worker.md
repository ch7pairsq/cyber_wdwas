# Worker 输出契约（附录 B.2）

你执行一个 Intent，只输出一个 JSON 对象，字段如下：

- task_id：分配给你的 intent_id。
- status：completed | partial | blocked | failed。
- summary：不超过 5 条高信号结论。
- facts：[{statement, confidence, evidence_ids}]，evidence_ids 必须是证据库已注册的 sha256。
- attempt：{strategy_key, preconditions, actions, outcome, failure_reason, ruled_out, next_best_action}。
- candidate_answer：仅在有证据支撑时非空。
- resource_usage：{wall_seconds, model_tokens, tool_calls}。

规则：strategy_key 稳定且必填；没有 evidence_id 的陈述只能写进 summary 并标注“假设”；
不符合契约将拒收并要求重写一次，仍失败记为 failed_contract。
