# misc.qr

前置：图片内含二维码/条码。

## 步骤
1. qr_decode（zbarimg -q --raw X）直接解码；失败先用 pngcheck/file 验证图片完整性。
2. 残缺或多块码：按位置标记排序拼接；图像增强需独立容器，本地不猜。
3. 解码结果是 URL 或嵌套 base64 时转 crypto.encoding。

## 产出
- 解码输出含 flag → 登记为证据；未解出 → ruled_out 记录已识别码数量。
