# crypto.encoding

前置：疑似 base64/hex/rot/xor 的文本或文件。

## 步骤
1. 字符集判断：hex 全 [0-9a-f]；base64 以 =/== 结尾；纯大写+符号先试 rot。
2. python3 单行完成 base64/hex 解码、rot-n 全量遍历、单字节 xor 爆破（按可打印率排序）。
3. openssl enc -d 支持常见对称算法；密钥/IV 常藏在描述或文件名。

## 产出
- 明文含 flag → 登记该解码命令的原始输出为证据；未解出 → ruled_out 记录已试编码与密钥假设。
