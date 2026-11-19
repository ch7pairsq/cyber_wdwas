# crypto.prng.mt19937

## 触发
- 输出像 32 位随机整数、token、验证码或时间种子。
- 题面出现 MT19937、Mersenne Twister、random、rand 或可预测状态。

## 最小动作
1. tool=python_exec
   code: 收集连续 32 位输出；用标准库实现 untemper，恢复 624 个状态字，再预测后续输出。
2. 时间种子：在题面时间附近 ±120 秒遍历 random.seed(t)，比较前几个输出。
3. 输出不足 624 个：先找额外泄露；不要对未知大范围种子盲爆破。

## 命中判据
- 预测值能解密/通过校验，或输出中逐字含唯一 flag{...}，且登记本次 stdout。
- 负判据：输出不是 32 位、状态不足、种子范围不可界定；停止。

## 何时换路
- 服务端 oracle 才给输出：转 arena_http，按一次请求一个样本收集。
- 不是 MT 而是 LCG/xorshift：换 PRNG 卡，不要套 624 状态。

## 踩坑
- Python random 与 C rand 的种子/序列不同，先确认实现语言。
- 每次取 32 位要 mask；浮点输出需要额外恢复步骤。

## 来源/边界
- 来源：ljagiello/ctf-skills@20d182d ctf-crypto/SKILL.md（MIT），人工蒸馏。
