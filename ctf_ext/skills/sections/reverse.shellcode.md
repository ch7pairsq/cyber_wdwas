# reverse.shellcode

前置：一段十六进制机器码或内存 dump，无 ELF 包装。

## 步骤
1. 用 disasm_bytes（cstool）按正确架构反汇编；x86 与 arm64 是常见选项。
2. 对照 syscall 号：write(1)/execve(59)；识别混淆字符串的 xor/add 常量。
3. 先解出字符串常量，再判断程序输出条件。
## 产出
- 还原后的输出若含 flag → 登记产生它的命令输出为证据；未解出 → ruled_out 记录架构与已排除变换。
