# pwn.stack.recon

## 触发
- 附件是 ELF，可能带 libc；需要先做栈/ROP 静态侦察。
- 最终利用执行必须有独立容器边界；本地没有边界时只做静态。

## 最小动作
1. tool=checksec path=... 记录 NX/PIE/Canary/RELRO。
2. tool=rop_gadgets path=... depth=8 找 pop/ret 等 gadget。
3. tool=strings path=... min_len=6 找 /bin/sh、格式串与提示。
4. tool=disassemble path=... 读漏洞函数，记录偏移、栈帧和寄存器约束。
5. 无执行边界：把 exploit 构思、偏移、所需 libc 写进 summary，不本地运行。

## 命中判据
- 静态阶段：给出可复核的偏移/gadget/保护，并登记 evidence_id。
- 执行阶段：授权容器内跑出唯一 flag{...}，登记输出。
- 负判据：保护不匹配或无执行边界；写 no_signal。

## 何时换路
- 有格式化字符串/堆题：停止本卡，记录新的利用面。

## 踩坑
- 不要在本机直接跑未知二进制；python_exec 也不是安全沙箱。
- PIE/Canary 开启时先找泄漏，不要盲打。
- 只静态侦察时不要伪造“已打通”。

## 来源/边界
- 不覆盖内核/浏览器 pwn。
