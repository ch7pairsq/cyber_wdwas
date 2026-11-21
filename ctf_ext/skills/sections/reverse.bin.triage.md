# reverse.bin.triage

## 触发
- 陌生 .bin/无扩展二进制：魔数不认识或混合数据，需先分路再动手。

## 最小动作
1. tool=file_info 读魔数；识别不出时 tool=hexdump 看头 64B 与结构（均匀性/对齐/尾部）。
2. tool=strings 扫明文（路径/URL/提示语）；tool=binwalk_scan 找嵌入签名与附加段。
3. tool=python_exec 算分块熵：高熵≈加密/压缩，低熵≈代码/明文，按分布切区。
4. 分路：代码 → tool=disasm_bytes 按架构试（先 x86/arm）；归档头 → 转 forensics 卡；判定加密 → 记特征转 crypto 卡。

## 命中判据
- 分路成功并进入对应卡流程；或直接读出唯一 flag{...}。
- 负判据：熵均匀且无签名；写 ruled_out 记熵值与已试架构。

## 何时换路
- 认出 PE 转 reverse.pe.exe；ELF 转 reverse.elf.static；.so 转 reverse.native.so；固件转 reverse.riscv.firmware。

## 踩坑
- 高熵不一定是加密：可能是压缩或真随机——先试解压再判加密。
- 架构不确定时先按 4 字节对齐找疑似指令边界，别盲扫。

## 来源/边界
- 内部编写；不涵盖调试器动态分析。
