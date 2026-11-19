# forensics.zip

## 触发
- 附件是 zip；关注伪加密、注释、隐藏数据、嵌套包。

## 最小动作
1. tool=archive_list path=... 看目录、注释、加密标记与压缩前后大小。
2. 伪加密：tool=python_exec
   code: 读本地副本文件头的 general purpose bit，print offset 与 flag；只读，不改原文件。
3. 隐藏数据：tool=binwalk_scan path=...；有嵌入文件转 forensics.archive_layers。
4. 注释/文件名线索：tool=strings path=... min_len=4；需要解包时 tool=archive_extract path=... dir=extracted。

## 命中判据
- 解包/解码输出含唯一 flag{...}，登记该次输出；或确认注释/伪加密位给出明文。
- 负判据：目录干净且无嵌入签名；写 ruled_out 已查通道。

## 何时换路
- 嵌套多：转 forensics.archive_layers。
- 伪加密/CRC 异常：先修本地头。

## 踩坑
- 不要用未注册的解压命令；统一走 archive_list/archive_extract。
- 伪加密只改回本地副本的标记位，不改原附件；证据记录读取偏移。
- 先看注释再看口令，避免无界尝试。

## 来源/边界
- 不覆盖 ZIP64 极端样本。
