# reverse.go

## 触发
- 附件是 Go 编译二进制：strings 可见 go1.x 与 runtime 线索、体积偏大、符号带 main.*。

## 最小动作
1. tool=file_info 与 tool=strings 确认 Go 版本与关键字符串（校验提示、错误消息）。
2. tool=symbols 找 main.* 函数；stripped 时 tool=python_exec 按 pclntab 结构恢复函数表（定位 magic 与函数名表）。
3. tool=disassemble 读 main.main 与校验函数；Go 1.17+ 用寄存器传参，注意参数/返回值位置与 C 不同。
4. 运算类校验用 tool=python_exec 复刻算法，逐段对照验证。

## 命中判据
- 复刻算法本地验证通过并产出唯一 flag{...}。
- 负判据：算法依赖缺失的外部数据；写 ruled_out。

## 何时换路
- 常规 ELF 静态分析转 reverse.elf.static；自定义 VM 转 reverse.vm.custom。

## 踩坑
- Go 字符串带长度头：strings 提取常被截断，关键常量按（指针，长度）成对从数据段读。
- goroutine/defer 打乱控制流：先按函数边界还原再读。

## 来源/边界
- 内部编写；不覆盖 cgo 与加壳混淆。
