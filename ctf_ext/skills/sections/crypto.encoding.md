# crypto.encoding

## 触发
- 文本/文件像 base64、hex、rot、单字节 xor；或解码链一层套一层。

## 最小动作
1. tool=hexdump path=... length=64 看头部字节。
2. tool=python_exec
   code: 依次尝试 base64/hex、rot-n 全遍历、单字节 xor，按可打印率打印 top 5；只用标准库。
3. 有密钥/IV 线索时：tool=strings path=... min_len=6 找描述、文件名里的提示；仍无法解密就转其他卡。

## 命中判据
- stdout 出现唯一 flag{...}，且能原样复现；登记该次输出。
- 负判据：全部候选不可读，或需要未知密钥；写 ruled_out 已试编码。

## 何时换路
- 出现 RSA/DH 参数：转 crypto.rsa.small_e 或 crypto.dh.degenerate。
- 只是编码层：不要继续猜密钥，先还原字节。

## 踩坑
- 本卡不覆盖需要密钥的对称解密。
- base64 先补齐 =；xor 要同时试单字节和多字节重复密钥。
- 不要只试一种编码就宣布无信号。

## 来源/边界
- 内部编写；只覆盖常见编码与单字节 xor。
