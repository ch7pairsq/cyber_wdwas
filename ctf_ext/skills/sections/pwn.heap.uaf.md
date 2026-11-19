# pwn.heap.uaf

## 触发
- 菜单式堆题（创建/删除/编辑/查看），删除后指针未清（UAF）或存在越界编辑；glibc 2.27-2.35 常见。

## 最小动作
1. tool=checksec 与 tool=file_info 看保护与 libc 版本；tool=disassemble 读菜单函数与堆块大小。
2. tool=python_exec 生成利用序列：UAF → tcache/fastbin 复用 → 改 fd 指向目标地址，再分配写入。
3. 按 glibc 版本选路：2.29+ 需处理 tcache key 与对齐检查；hook 已移除的版本走 FSOP 或先泄漏栈地址。
4. 远程打通后读输出提取 flag。

## 命中判据
- shell 输出或读文件结果含唯一 flag{...}。
- 负判据：泄漏地址后偏移对不上；写 ruled_out 本地 libc 与远程版本差异。

## 何时换路
- 纯栈溢出转 pwn.stack.recon；存在格式化字符串优先用其任意读写。

## 踩坑
- 本地与远程 libc 不一致时先泄漏再定版本，不要盲套偏移。
- chunk 大小与对齐错一位就崩；每次分配记录 size 字段。
- 禁用 tcache 的环境直接走 fastbin/unsorted 路线。

## 来源/边界
- 内部编写；不覆盖 V8 与内核堆。
