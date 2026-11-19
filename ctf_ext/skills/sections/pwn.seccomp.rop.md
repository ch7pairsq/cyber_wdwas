# pwn.seccomp.rop

## 触发
- 二进制或题面含 seccomp 过滤（BPF 装载、系统调用白名单）；execve 被禁但 open/read/write 常放行。

## 最小动作
1. tool=checksec 与 tool=file_info 看保护与架构；tool=disassemble 读过滤初始化与主逻辑。
2. tool=python_exec 从过滤规则推断系统调用白名单（openat/mmap/sendfile 变体都要考虑）。
3. tool=rop_gadgets 搜 gadget，python_exec 拼链：open("/flag")→read→write 输出到标准流。
4. 无 stdout 通道时改用回显侧信道（错误差异/时序）逐字节判定。

## 命中判据
- 目标流输出唯一 flag{...}。
- 负判据：open 也被禁；写 ruled_out 已试系统调用清单。

## 何时换路
- 无过滤直接 shell 转 pwn.stack.recon；堆入口转 pwn.heap.uaf。

## 踩坑
- 32/64 位系统调用号与参数寄存器顺序不同，先确认架构再拼链。
- write 的 fd 用 read 返回值，别硬编码。

## 来源/边界
- 内部编写；不覆盖内核提权。
