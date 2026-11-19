# forensics.stego.image

## 触发
- 附件是 png/jpg/bmp；怀疑 LSB、元数据、尾部附加数据或隐写提取。

## 最小动作
1. tool=file_info path=...；tool=png_check path=... 看结构异常。
2. tool=metadata path=... 看注释/作者/软件字段。
3. tool=binwalk_scan path=... 找尾部附加数据；有嵌入文件转 archive_layers。
4. tool=steghide_extract path=... passphrase= 先用空串，再试题目提示词；输出 stego.out。
5. 图片内二维码：tool=qr_decode path=...。

## 命中判据
- 任一提取/解码输出含唯一 flag{...}，登记对应 evidence_id。
- 负判据：结构正常、无附加签名、空口令失败；写 ruled_out 已试通道。

## 何时换路
- 提取到 pcap/PDF：转对应 forensics 卡。
- 需要密码且无提示：停止并记录缺口。

## 踩坑
- 提取工具的口令先空串，不要一上来爆破。
- 不要用未注册的图像工具；需要像素级处理时用 python_exec，并说明依赖。
- 改扩展名不代表真实格式，先 file_info。

## 来源/边界
- 内部编写；不覆盖盲 LSB 全量扫描。
