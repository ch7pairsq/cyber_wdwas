# reverse.riscv.firmware

## 触发
- 附件是固件镜像/OTA 包（多段、头部版本字段）或 RISC-V ELF。

## 最小动作
1. tool=binwalk_scan 看镜像分段（bootloader/应用/文件系统），tool=binwalk_extract 解出内层。
2. RISC-V ELF：tool=disassemble 按架构读逻辑；裸机 bin 先确认加载基址再反汇编。
3. OTA 包：tool=archive_list 看清单与签名字段；校验逻辑常在 bootloader，找验证与降级检查绕过点。
4. tool=python_exec 解析自定义头/差分包结构，还原目标文件或直接读内嵌 flag。

## 命中判据
- 解出内嵌唯一 flag{...}，或绕过校验得到目标产物。
- 负判据：镜像加密且密钥不在包内；写 ruled_out 分段与加密迹象。

## 何时换路
- 内层是标准 ELF 转通用静态分析；文件系统里有图片转 forensics.stego.image。

## 踩坑
- 裸机 bin 反汇编前必须确认基址，否则全是伪指令。
- OTA 元数据里的版本与签名常是解题入口，先读头部。

## 来源/边界
- 内部编写；不覆盖安全启动密码学细节。
