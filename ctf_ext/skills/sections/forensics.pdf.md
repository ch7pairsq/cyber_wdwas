# forensics.pdf

前置：PDF 附件。

## 步骤
1. pdf_text（pdftotext）提取正文；正文为空时怀疑文本层缺失或字体混淆。
2. metadata（exiftool）看 Author/Producer/注释；binwalk_scan 查嵌入图片与附件。
3. 提取出的内层文件回到对应 forensics 技能继续分析。

## 产出
- 任意提取命令输出含 flag → 登记为证据；未解出 → ruled_out 记录已提取层。
