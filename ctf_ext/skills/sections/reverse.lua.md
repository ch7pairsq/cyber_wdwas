# reverse.lua

## 触发
- 附件是 Lua 字节码（luac 文件头）、Lua 脚本混淆（乱码变量名/编码字符串）或游戏逻辑题带 lua 字样。

## 最小动作
1. tool=file_info 与 tool=hexdump 确认字节码版本头；纯脚本先 tool=strings 找可读线索。
2. 字节码：tool=python_exec 按版本格式解析常量表与操作码（格式简单，手写解析可行）。
3. 混淆脚本先解字符串层（base64/hex/转义），还原控制流后再读逻辑。
4. 找校验/编码函数复刻求解，输出与程序一致后导出 flag。

## 命中判据
- 复刻逻辑求出的输入输出唯一 flag{...}。
- 负判据：自定义 opcode 过深；写 ruled_out 版本与还原进度。

## 何时换路
- 逻辑是 VM 结构转 reverse.vm.custom；附件实为压缩包转 forensics.archive_layers。

## 踩坑
- 不同 Lua 版本字节码不兼容，先确认版本再按格式解析。
- 游戏类题逻辑常在数据文件，先扫资源再读代码。

## 来源/边界
- 内部编写；不覆盖 JIT 层语义。
