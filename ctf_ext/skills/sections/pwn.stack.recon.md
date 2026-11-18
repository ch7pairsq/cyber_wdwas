# pwn.stack.recon

前置：ELF（可能附 libc）；最终执行需独立容器边界。

## 步骤
1. checksec --file=X 确定 NX/PIE/Canary/RELRO。
2. rop_gadgets（ROPgadget --depth 8）找 gadget；strings 找 /bin/sh 与提示。
3. disassemble（objdump -M intel）读漏洞函数，确定偏移与寄存器约束。
4. 沙箱不可用时只做静态侦察：把 exploit 构思与所需偏移写进 summary，不本地执行。

## 产出
- 在允许的执行边界内跑出 flag → 登记该命令输出为证据；仅静态侦察 → outcome=no_signal 并写 next_best_action。
