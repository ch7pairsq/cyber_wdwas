# reverse.vm.custom

## 触发
- 二进制/脚本里有 opcode 数组、dispatch 循环、栈或寄存器机。
- 题面出现 VM、bytecode、interpreter、自定义指令集。

## 最小动作
1. tool=strings path=... min_len=4；tool=disassemble path=... 找 dispatch 与 opcode 表。
2. tool=python_exec
   code: 提取 opcode 表和 handler 偏移，写一个最小解释器；逐条打印 pc、opcode、栈顶，先跑已知输入。
3. 有机器码片段时用 tool=disasm_bytes arch=x86/arm64 交叉验证；不要直接执行未知二进制。

## 命中判据
- 解释器输出与题目校验一致，或还原出唯一 flag{...}；登记工具输出。
- 负判据：opcode 表不完整、需要未提供的运行时；停止并记录缺口。

## 何时换路
- 实际是 Python code object：转 reverse.bytecode.python。
- 是原生混淆/反调试：转 reverse.elf.static，只做静态路径。

## 踩坑
- 先确认立即数字节序、有符号扩展和 pc 自增规则。
- 不要用真实恶意样本在本机执行；只做静态解释。

## 来源/边界
- 来源：ljagiello/ctf-skills@20d182d ctf-reverse/SKILL.md（MIT），人工蒸馏。
