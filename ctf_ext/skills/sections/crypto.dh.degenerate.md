# crypto.dh.degenerate

## 触发
- 题面/附件给出 DH 参数 g、p，或 X25519 peer public。
- 先观察 g=0/1/p-1、peer public=0/1、共享密钥是否全零或常数。

## 最小动作
1. tool=python_exec
   code: 只算 pow(g,a,p) 或 X25519 共享值；打印 g、peer 与共享值的十六进制，先判断是否退化。
2. 共享值为全零/常数：直接用该值尝试解密密文，标准库即可。
3. 参数正常：记录观察，转离散对数或协议分析，不在本卡硬跑。

## 命中判据
- 解密输出逐字含唯一 flag{...}，且来自本次工具输出。
- 负判据：g 正常、peer 非小阶、共享值非常数；停止本卡。

## 何时换路
- 全零共享值更像协议跳过校验，不是离散对数。
- p-1 光滑时转 Pohlig-Hellman；否则先找实现缺陷。

## 踩坑
- 不要先跑重型 DLP；退化值优先。
- X25519 有 cofactor/clamping；u=0/1 先试。

## 来源/边界
- 来源：ljagiello/ctf-skills@20d182d ctf-crypto/SKILL.md（MIT），人工蒸馏，未复制命令。
