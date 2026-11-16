# 交接包（handoff pack，§7、§9）

新 Worker 接手时按序读取：
1. open_intents：避免重复领取与重复策略。
2. recent_attempts：已尝试策略、失败原因、已排除方向、下一步。
3. lessons：有效教训（superseded=0）。
4. evidence 指针：只引用 evidence_id，不复制原文。

禁止：凭记忆重建状态；私有上下文可随时丢弃，共享板是唯一事实来源。
