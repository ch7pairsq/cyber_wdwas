# misc.unicode

## 触发
- 题面/文件含奇怪字符：同形字、全角、组合字符、零宽字符、双重编码。

## 最小动作
1. tool=hexdump 看原始字节，识别 UTF-8 多字节序列与 BOM。
2. tool=python_exec 用 unicodedata 做 NFC/NFD 归一化对比、逐 codepoint 打印、零宽字符提取（U+200B/200C/200D/FEFF 当 0/1）。
3. 编码链（punycode/idna、UTF-7、双重百分号编码）逐层解，输出中间结果比对。

## 命中判据
- 解码链产出唯一 flag{...}。
- 负判据：字符只是显示差异不携带信息；写 ruled_out。

## 何时换路
- 纯编码链转 crypto.encoding；隐写载体转 forensics.stego.image。

## 踩坑
- 终端显示不等于真实字节：一切以 hexdump 字节为准。
- 归一化会改变长度：先保存原始串再处理。

## 来源/边界
- 内部编写；不涵盖字体渲染类题型。
