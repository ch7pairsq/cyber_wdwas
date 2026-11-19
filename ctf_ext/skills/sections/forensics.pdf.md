# forensics.pdf

## 触发
- 附件是 PDF；需要文本层、元数据、内嵌图片/附件。

## 最小动作
1. tool=pdf_text path=... 提取正文；为空时转第 3 步。
2. tool=metadata path=... 看 Author/Producer/注释。
3. tool=binwalk_scan path=... 查嵌入对象；tool=pdf_images path=... mode=list 看图片清单，mode=extract 抽取。
4. 抽出的图片用 tool=ocr_text path=... lang=eng+chi_sim；二维码用 tool=qr_decode path=...。

## 命中判据
- 文本/OCR/解码输出含唯一 flag{...}，且能定位到来源页/对象；登记 evidence_id。
- 负判据：文本层为空且无嵌入对象；写 ruled_out 已提取层。

## 何时换路
- 附件是图片/二维码：转 forensics.stego.image 或 misc.qr。
- 只有文本：转 crypto.encoding 或直接读。

## 踩坑
- 扫描版 PDF 不要只跑文本提取；先看图片清单。
- 字体混淆时文本可能乱码，转 OCR 或看对象流。
- 不要把 PDF 当压缩包递归解；只取声明的嵌入对象。

## 来源/边界
- 内部编写；不覆盖恶意 PDF 利用。
