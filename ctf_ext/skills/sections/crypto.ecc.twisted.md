# crypto.ecc.twisted

## 触发
- 曲线是 Twisted Edwards 形式（x²+y²=1+dx²y²）或题面自定义曲线参数。

## 最小动作
1. tool=python_exec 实现点加与标量乘（Edwards 统一公式），先小样例自验。
2. 按参数选攻击：小阶子群点、低阶曲线、无效曲线/无效点注入、标量泄漏。
3. 签名/密钥交换题先验证是否校验点属于主群——不校验则子群攻击直接恢复密钥。
4. 恢复量按题目映射解码。

## 命中判据
- 恢复密钥/消息经服务端或本地验证器确认，解码唯一 flag{...}。
- 负判据：安全素数阶且校验完整；写 ruled_out 已试攻击面。

## 何时换路
- 同源类转 crypto.csidh；RSA 与格转 crypto.rsa.variants、crypto.lattice.lll。

## 踩坑
- Edwards 与 Weierstrass 互转易错，全程用同一形式推导。
- 自定义曲线先验证群律成立再谈攻击。

## 来源/边界
- 内部编写；不覆盖 pairing 构造。
