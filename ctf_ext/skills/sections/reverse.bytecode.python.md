# reverse.bytecode.python

## 触发
- 附件是 .pyc、marshal 数据、PyInstaller 解包后的 PYZ，或题面要求分析 Python 字节码。
- 可观察信号：文件头 magic、co_consts、dis 输出、__pycache__。

## 最小动作
1. tool=file_info path=...；tool=hexdump path=... length=64 看 magic 与版本。
2. tool=python_exec
   code: 用 marshal.loads 读取 code object，dis.dis 反汇编；递归打印 co_consts 中的字符串和嵌套 code。
3. 版本不匹配时先修正 magic/解释器版本，不要强行反序列化。

## 命中判据
- 从常量、比较逻辑或反汇编还原出唯一 flag{...}，并能定位到 code object。
- 负判据：字节码损坏或版本不支持；停止，转静态十六进制分析。

## 何时换路
- 逻辑在自定义 VM/打包器：转 reverse.vm.custom。
- 是原生 ELF/PE：转 reverse.elf.static，不要继续找 Python 常量。

## 踩坑
- 不要 import/执行未知 .pyc；只用 marshal + dis 读取。
- PyInstaller 先定位解包目录，再分析入口脚本的 code object。

## 来源/边界
- 来源：ljagiello/ctf-skills@20d182d ctf-reverse/SKILL.md（MIT），人工蒸馏。
