# forensics.archive_layers

前置：多层压缩/嵌套文件；口令可能在注释、文件名或上一层输出里。

## 步骤
1. archive_list（7z l -slt）看格式、注释与加密标记；zip 伪加密查 general purpose bit。
2. archive_extract 解到子目录后，对内层文件重复侦察；嵌套镜像用 binwalk_extract。
3. 口令来源：上一层注释、文件名变形、题目名。

## 产出
- 逐层解到明文 → 登记最终解包/解码命令输出为证据；未解出 → ruled_out 记录层数与卡点。
