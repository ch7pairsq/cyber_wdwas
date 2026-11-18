# forensics.stego.image

前置：png/jpg/bmp 附件。

## 步骤
1. file 确认真实格式；pngcheck -v 查 PNG 结构异常。
2. metadata（exiftool）看注释字段；binwalk_scan 找尾部附加数据。
3. steghide_extract 用空口令优先，再试题目提示词；输出文件在 stego.out。
4. 图片内二维码直接 qr_decode（zbarimg --raw）。

## 产出
- 提取/解码输出含 flag → 登记对应命令输出为证据；未解出 → ruled_out 记录已试通道与口令。
