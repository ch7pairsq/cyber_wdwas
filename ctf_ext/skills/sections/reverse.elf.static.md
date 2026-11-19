# reverse.elf.static

## 触发
- 附件是 ELF（可能 stripped），需要静态看逻辑；无需执行。

## 最小动作
1. tool=file_info path=... 确认架构与 stripped。
2. tool=checksec path=... 记录 NX/PIE/Canary/RELRO。
3. tool=strings path=... min_len=6 找提示、格式串与模板。
4. tool=elf_headers path=... 看入口与节；tool=symbols path=... 看未 strip 符号。
5. tool=disassemble path=... 从 main/关键函数读比较与常量；十六进制片段用 tool=disasm_bytes arch=x86 hex=...

## 命中判据
- 由工具输出还原出唯一 flag{...}，且能给出对应 evidence_id；或定位到校验函数与常量。
- 负判据：只有打包/混淆，静态读不出；写 ruled_out 缺口。

## 何时换路
- 发现 Python/marshal：转 reverse.bytecode.python。
- 出现 opcode dispatch：转 reverse.vm.custom。

## 踩坑
- 不要直接执行未知二进制；本卡只做静态。
- stripped 时先看入口与字符串交叉引用，不要从 ELF 头硬猜。
- 识别到编码常量转 crypto.encoding，不在本卡硬解。

## 来源/边界
- 内部编写；不覆盖动态调试与反调试。
