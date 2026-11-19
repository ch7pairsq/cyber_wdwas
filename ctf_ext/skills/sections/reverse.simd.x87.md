# reverse.simd.x87

## 触发
- 反汇编大量向量指令（SSE/AVX，ymm/zmm 寄存器）或 x87 浮点栈指令（fld/fmul/fistp 类），计算被向量化/浮点化。

## 最小动作
1. tool=disassemble 读向量块，tool=disasm_bytes 对疑难单条确认编码；记录寄存器宽度与数据布局。
2. tool=python_exec 逐条复刻语义：向量化按 lane 展开，x87 按 80 位栈机语义模拟。
3. 关注精度差异（浮点舍入、整数转换截断）——答案常差在舍入位。
4. 复刻后按程序输入流验证输出一致再求 flag。

## 命中判据
- 复刻计算与程序输出一致并导出唯一 flag{...}。
- 负判据：语义不明指令过多；写 ruled_out 已确认指令集。

## 何时换路
- 向量块只是外层混淆、核心是 VM 转 reverse.vm.custom；整块是编码表转 crypto.encoding。

## 踩坑
- AVX 掩码与 permute 类指令语义复杂，逐条查语义再复刻，不要凭记忆猜。
- x87 舍入模式影响结果，先还原状态寄存器。

## 来源/边界
- 内部编写；不覆盖 GPU/协处理器语义。
