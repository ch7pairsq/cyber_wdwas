# misc.pt.zipfile

## 触发
- 附件是 .pt/.pth/.ckpt（PyTorch checkpoint）或 pickle 序列化模型文件；常见于迷宫/游戏/AI 题。

## 最小动作
1. tool=file_info 确认容器格式；zip 容器按 zip 读，老格式是纯 pickle 流。
2. tool=python_exec 列出成员并安全解析张量字典（只取基本类型与 state_dict 的数组数据，不加载模型类）。
3. 对张量做题目逻辑（迷宫寻路、权重还原、逐层解码），输出编码后导出 flag。

## 命中判据
- 处理结果含唯一 flag{...}。
- 负判据：张量与题面逻辑对不上；写 ruled_out 已试的解码顺序。

## 何时换路
- 容器里是普通图片/文本转 forensics.archive_layers；权重是编码表转 crypto.encoding。

## 踩坑
- pickle 只取基本类型字段，自定义类一律按 dict 处理，避免反序列化副作用。
- 大张量先看 shape 再切片，不要一次展开全部。

## 来源/边界
- 内部编写；不覆盖训练与推理加速。
