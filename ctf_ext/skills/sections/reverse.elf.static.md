# reverse.elf.static

前置：ELF 二进制（可能 stripped）；无执行环境要求。

## 步骤
1. file 确认架构与 stripped；checksec --file=X 看 NX/PIE/Canary/RELRO。
2. strings -a -n 6 X 找提示、格式串与 flag 模板。
3. readelf -h -l -S X 看入口与节；nm -C X 列符号（未 strip 时）。
4. objdump -d -M intel X 从 main 与关键函数读逻辑；裸十六进制片段用 cstool。
5. 识别到常量表/编码变换时，转 crypto 技能求解，不要原地硬猜。

## 产出
- 解出明文 → 用能原样输出该 flag 的命令登记证据再作答；未解出 → ruled_out 写「静态逻辑已读，缺口是 X」。
