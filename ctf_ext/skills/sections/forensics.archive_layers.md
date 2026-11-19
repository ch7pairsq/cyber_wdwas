# forensics.archive_layers

## 触发
- 多层压缩/嵌套文件；口令可能在注释、文件名或上一层输出。

## 最小动作
1. tool=archive_list path=... 看格式、注释与加密标记。
2. tool=archive_extract path=... dir=layer1 解到子目录；对内层文件重新 tool=file_info path=layer1/...
3. 嵌套镜像：tool=binwalk_scan path=...；需要提取时 tool=binwalk_extract path=...
4. 口令来源：上一层注释、文件名变形、题目名；每试一个记 ruled_out。

## 命中判据
- 逐层解到明文，输出含唯一 flag{...}；登记最终解包/解码输出。
- 负判据：层数/口令卡住；记录已试层数与口令假设。

## 何时换路
- 解出 pcap/PDF/图片：转对应 forensics 卡。
- 需要密码且无提示：停止并记录缺口。

## 踩坑
- 不要递归解压到工作区根目录；每层用独立 dir。
- 不要无界爆破口令；先试题目给出的线索。
- 内层文件可能不是压缩包，先 file_info 再决定。

## 来源/边界
- 内部编写；不覆盖加密容器破解。
