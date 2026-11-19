# pwn.mips64

## 触发
- 附件是 MIPS/MIPS64 ELF（file 报 mips），或题面提到嵌入式/路由器架构。

## 最小动作
1. tool=file_info 确认架构与字节序；tool=elf_headers 看类型与节。
2. tool=disassemble 按 MIPS 读主逻辑；注意分支延迟槽语义。
3. tool=rop_gadgets 可用时搜 gadget；栈参数布局按 o32/n64 ABI 区分。
4. tool=python_exec 按大小端打包 payload，远程交互按题目协议执行。

## 命中判据
- 服务端返回唯一 flag{...}。
- 负判据：缺运行环境无法本地验证；写 ruled_out 并改远程直接试。

## 何时换路
- 附件是固件包先 tool=binwalk_extract 解内层再分析；arm 架构走通用栈/堆流程。

## 踩坑
- 延迟槽让相邻指令顺序看似颠倒，gadget 选取要跨槽验证。
- 先泄漏 libc 基址再谈利用，别盲套本地偏移。

## 来源/边界
- 内部编写；不覆盖 MIPS 内核漏洞。
