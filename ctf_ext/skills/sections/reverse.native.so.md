# reverse.native.so

## 触发
- 附件含 .so（JNI 配套、libc 替身、插件/补丁库）：要还原算法或挂进利用链。

## 最小动作
1. tool=file_info 确认架构；tool=elf_headers 看导出与依赖（JNI_OnLoad/Java_* 即 JNI 库）。
2. tool=symbols + tool=disassemble 读导出函数；JNI 混合题先 tool=java_decompile 看 Java 侧调用约定，再对齐 native 侧。
3. 运算校验 tool=python_exec 复刻；或用 CDLL 直接加载调用导出函数验证。
4. 利用链场景：tool=python_exec 构造本地运行环境（LD_LIBRARY_PATH/patchelf）挂替代 .so 验证；GOT 劫持思路转 pwn 卡。

## 命中判据
- 复刻/调用验证通过并产出唯一 flag{...}。
- 负判据：导出无关键函数或算法依赖外部状态；写 ruled_out。

## 何时换路
- 主程序分析转 reverse.elf.static；栈溢出利用转 pwn.stack.recon。

## 踩坑
- JNI 类型映射（jint/jstring ↔ Java 类型）先确认，别按 C 猜。
- 符号可见≠可达：真入口可能走 RegisterNatives 动态注册，先查再读。

## 来源/边界
- 内部编写；不涵盖内核模块与驱动。
