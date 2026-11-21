# crypto.aes.nonce_reuse

## 触发
- AES-CTR/OFB/GCM 复用 nonce/IV：两段密文异或呈"两明文异或"特征（可读性拼接感）；GCM 报认证错。

## 最小动作
1. tool=python_exec 异或两段密文得明文异或，用已知明文片段（HTTP 头、JSON 键、常见英文） crib-drag 恢复另一段。
2. 确认 nonce 结构：CTR 的 nonce+counter 划分（8+8 或 12+4）决定 counter 进位是否跨块。
3. 密钥流恢复后解任意同密钥密文；完整推导过程留 evidence 供复核。

## 命中判据
- 恢复出唯一 flag{...}。
- 负判据：nonce 不复用或无法 crib；写 ruled_out。

## 何时换路
- 非 AES 的 RSA 问题转 crypto.rsa.small_e；纯编码转 crypto.encoding。

## 踩坑
- GCM 无 padding，明文长度等于密文长度；不要按块补齐猜。
- crib-drag 按位置滑动匹配，命中判据是"两侧同时可读"。

## 来源/边界
- 内部编写；不涵盖 CBC 位翻转与其他模式误用。
