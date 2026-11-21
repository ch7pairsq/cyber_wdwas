# forensics.stego.audio

## 触发
- 附件是音频（wav/mp3/flac），提示"听/藏着/声音里有"或频谱异常，密钥或 flag 藏在音频里。

## 最小动作
1. tool=media_probe 看编码/时长/声道；tool=hexdump 查文件尾附加数据与非常规块。
2. tool=steghide_extract 逐口令试：空串→题面关键词；mp3/flac 无效属正常，转数值路径。
3. tool=python_exec 数值分析：wave 读采样，LSB 逐位提取（声道/位序变体）；短时能量/过零率找 Morse 节奏。
4. 频谱藏字：FFT 找异常频段，渲染成 PNG 后 tool=ocr_text 识别；倒放先反转采样再走上述步骤。

## 命中判据
- 解出唯一 flag{...}。
- 负判据：各路径无信号；写 ruled_out 记录已试载体/口令/频段。

## 何时换路
- 图片载体转 forensics.stego.image；纯编码转 crypto.encoding。

## 踩坑
- 模型看不了图：一律"数值分析→渲染 PNG→ocr_text"，别假设自己能看频谱。
- 频谱藏字常在 4kHz 以上，渲染范围留足；LSB 位序/声道有变体，失败再换序。

## 来源/边界
- 内部编写；不涵盖语音内容识别。
