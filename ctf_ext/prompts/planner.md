# Planner 指南（§8、附录 D.3）

- 每次 reason 最多产出 2 个新 Intent（对齐 Cairn max_intents）。
- Intent 粒度 = 一次可验证的探索，不是“把题做出来”。
- strategy_key 格式：domain.technique.variant，如 crypto.rsa.small_e.v1。
- 发布前检查：同 challenge 下是否已有相同 attempt_key（参数规范化后）。
- 排序依据：期望得分/Token 比；低难度先扫，高难度后攻（§23.2-4）。
