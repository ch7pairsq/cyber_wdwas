# misc.qr

## 触发
- 图片内含二维码/条码，或题面要求扫码。

## 最小动作
1. tool=qr_decode path=... 直接解码。
2. 失败：tool=file_info path=... 与 tool=png_check path=... 验证格式和完整性。
3. 解码结果是 URL/base64/hex：转 crypto.encoding；是嵌套图片：转 forensics.stego.image。

## 命中判据
- 解码原文含唯一 flag{...}，登记该次输出。
- 负判据：图片无码或严重残缺；写 ruled_out 已识别码数量。

## 何时换路
- 扫码得到编码文本：转 crypto.encoding。
- 得到 URL/文件：转 forensics 对应卡。

## 踩坑
- 不要用未注册的扫码命令；统一走 qr_decode。
- 多码图片逐张解，不要只取第一张。
- 残缺码不要本地盲修；说明缺口并转人工/下一意图。

## 来源/边界
- 内部编写；不覆盖图像增强。
