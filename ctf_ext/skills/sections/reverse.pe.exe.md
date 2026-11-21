# reverse.pe.exe

## 触发
- 附件是 PE（.exe/.dll，MZ 头）Windows 二进制：还原算法或提取密钥。

## 最小动作
1. tool=file_info 确认 PE 与位数；elf_headers/checksec 是 ELF 专用，PE 上必败，别调。
2. tool=symbols 看 COFF 符号；tool=hexdump 看 PE 头/节表；tool=python_exec 解析导入表找关键 API（Crypto*/AntiDebug 特征）。
3. tool=disassemble 反汇编；失败时 tool=hexdump 取 .text 字节，tool=disasm_bytes 按架构反汇编交叉验证。
4. 加壳探测：高熵节区+极少导入 → 记 ruled_out（无脱壳工具，别空耗）；明文逻辑用 tool=python_exec 复刻。

## 命中判据
- 算法复刻通过并产出唯一 flag{...}。
- 负判据：加壳/虚拟化保护；写 ruled_out 记录节区熵与导入特征。

## 何时换路
- ELF 转 reverse.elf.static；.so 场景转 reverse.native.so。

## 踩坑
- 反汇编地址对 PE 重定位可能偏移，与 disasm_bytes 结果互验。
- Windows API 语义（宽字符/句柄/结构体）别按 POSIX 猜。

## 来源/边界
- 内部编写；不涵盖脱壳与 VM 保护还原。
