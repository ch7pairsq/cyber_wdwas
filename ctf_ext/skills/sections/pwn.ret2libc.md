# pwn.ret2libc

## 触发
- 栈溢出已确认（先过 pwn.stack.recon），NX 开启；二进制自带 libc 或已获得服务端 libc 版本。

## 最小动作
1. 有泄露点：tool=python_exec 用 pwntools 构造 ROP 泄露 got 表项（如 puts），由符号偏移算出 libc 基址。
2. 组第二段链：padding → pop_rdi → binsh 地址 → ret 对齐 → system；无 binsh 字符串时先写入或用 one_gadget 思路。
3. 第二轮连入执行读 flag；把两段链与基址计算登记 evidence。

## 命中判据
- 执行边界内拿到唯一 flag{...}。
- 负判据：偏移错或基址异常（为 0/对齐差）；重算后写 ruled_out。

## 何时换路
- 还没有泄露点先转 pwn.stack.recon；system 被禁转 pwn.seccomp.rop 的 ORW 思路。

## 踩坑
- 64 位注意 movaps 栈对齐，必要时多垫一个 ret。
- 本地 libc 与服务端不一致时以服务端版本为准，偏移不要混用。
- 泄露值要去掉换行并按小端解析后再算基址。

## 来源/边界
- 内部编写；以 64 位 x86 为主，32 位仅思路。
