# forensics.zip

前置：zip 附件；关注伪加密、注释、隐写。

## 步骤
1. unzip -l 查看目录结构与加密标记。
2. 检查 general purpose bit 与压缩前后大小差（伪加密）。
3. strings + binwalk 检查隐藏数据；注释位查看。

## 产出
- 找到 flag → 直接提交；未找到 → ruled_out 写入「非伪加密/非注释隐写」。
