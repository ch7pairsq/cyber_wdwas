# crypto.rsa.small_e

前置：已知 n、e，密文 c；e 很小（如 3、5）且明文短。

## 步骤
1. 计算 c 的 e 次方根（用 gmpy2.iroot 或二分整数根）。
2. 若不成立，尝试不 padding 的多组密文共模（Hastad）。
3. 记录：n 位数、e 值、是否 padding。

## 产出
- 成立 → flag 直接得出；不成立 → ruled_out 写入「非小指数明文」。
