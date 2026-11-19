# crypto.rsa.common_modulus

## 触发
- 同一 n 下有多组 (e_i, c_i)，且明文相同；常见于广播或密钥复用题。
- 先算 gcd(e1,e2)；互素时才有直接的 Bezout 组合。

## 最小动作
1. tool=python_exec
   code: 用扩展欧几里得求 a*e1+b*e2=1；计算 m=pow(c1,a,n)*pow(c2,b,n) mod n。
2. 负指数用模逆；结果转字节，检查可打印文本。
3. 若 gcd(e1,e2)>1：不要硬套；记录公因子，转小指数/分解路径。

## 命中判据
- 解出的 m 字节逐字含唯一 flag{...}，且来自本次工具输出。
- 负判据：n 不同、明文不同、padding 未知或 gcd>1；停止本卡。

## 何时换路
- e 很小且单组密文：转 crypto.rsa.small_e。
- n 可分解或有多组共享素数：转分解/批量 gcd 路径。

## 踩坑
- 只适用于无 padding、同一明文的 RSA 教学题。
- 不要把不同题目的 n/c 混在一起；先确认参数同源。

## 来源/边界
- 来源：ljagiello/ctf-skills@20d182d ctf-crypto/SKILL.md（MIT），人工蒸馏。
