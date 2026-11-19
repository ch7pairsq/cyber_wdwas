# reverse.rust

## 触发
- ELF 含 Rust 特征（panic 路径、core:: 命名、mangled 符号）或题面注明 Rust。

## 最小动作
1. tool=symbols 列符号找 main 与校验函数（Rust 符号带哈希后缀）。
2. tool=disassemble 读校验逻辑；迭代器与泛型会内联展开，抓核心比较点，不还原源码级结构。
3. tool=strings 定位字符串与常量，tool=python_exec 复刻校验算法。
4. 大整数运算与迭代器链逐段还原并验证。

## 命中判据
- 求出的输入被程序接受并输出唯一 flag{...}。
- 负判据：核心逻辑在闭包链中过深；写 ruled_out 已还原函数。

## 何时换路
- 校验是向量/浮点计算转 reverse.simd.x87；VM 结构转 reverse.vm.custom。

## 踩坑
- Rust 无稳定 ABI，别按 C 程序习惯找 main 参数。
- panic 分支的报错文本常含校验线索，先读报错再硬啃。

## 来源/边界
- 内部编写；不覆盖 async 运行时细节。
