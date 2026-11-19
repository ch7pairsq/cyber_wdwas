# crypto.rsa.small_e

## 触发
- 附件给出 n、e、c；e 小，c < n，明文短。
- 或同一明文多组 (n_i,c_i)，e 相同。
- 不适用：e 很大、c >= n、padding/OAEP 未知。

## 最小动作
1. tool=python_exec
   code: 用 math.isqrt 或整数二分求 c 的 e 次根，print 整数 m 和 m 的字节解码；只用标准库。
2. 若失败：tool=python_exec
   code: 多组密文时对 i!=j 求 gcd(c_i**e-c_j**e, n_i)，检查共模；单组不要硬套。
3. 若仍失败：tool=evidence_digest
   args: evidence_ids 填前面相关输出，复核 n/e/c 是否同一题；再转大数分解或 encoding。

## 命中判据
- 本次 stdout 逐字出现唯一 flag{...}，且已登记 evidence_id。
- 负判据：根非整数、解码不可读、c>=n；停止并写 ruled_out。

## 何时换路
- 同 n 多组 e/c：转 crypto.rsa.common_modulus。

## 踩坑
- 不要用浮点 pow(c, 1/e)，精度不够；只用整数根。
- 单组密文不要套 Hastad；多组同 e 才考虑共模。
- 候选答案只提交一次，未复核前写进 summary，不写成已证实事实。

## 来源/边界
- 只覆盖无 padding 教学题，不覆盖 OAEP。
