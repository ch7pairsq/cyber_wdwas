# reverse.shellcode

## 触发
- 十六进制机器码/内存 dump，无 ELF 包装；需要按架构反汇编。

## 最小动作
1. tool=disasm_bytes arch=x86 hex=<机器码>；ARM64 用 arch=arm64。
2. tool=python_exec
   code: 按 syscall 号整理 write/execve，尝试 xor/add 常量还原字符串；print 候选明文。
3. 有文件偏移/内存地址时：tool=hexdump path=... length=... 对照原始字节。

## 命中判据
- 还原出的字符串或输出含唯一 flag{...}，且能追溯到本次工具输出；登记 evidence_id。
- 负判据：架构不匹配或字节长度奇数；先修正输入，不硬猜。

## 何时换路
- 需要还原完整 ELF 行为：转 reverse.elf.static。
- 指令流是自定义 VM：转 reverse.vm.custom。

## 踩坑
- 先确认架构，x86 与 arm64 的 syscall 号不同。
- 不要执行 shellcode；只做反汇编和静态还原。
- 字符串可能分段异或，按循环逐字节还原。

## 来源/边界
- 内部编写；不覆盖自修改代码。
