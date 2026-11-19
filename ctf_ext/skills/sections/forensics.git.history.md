# forensics.git.history

## 触发
- 附件含 .git 目录/仓库包，或题面提到历史版本、被删文件、来源溯源。

## 最小动作
1. tool=file_info 确认结构；裸 .git 直接分析，压缩包先 tool=archive_extract 解开。
2. tool=git_history mode=log 看全分支提交与文件变动（找删除/改名记录）。
3. mode=fsck 找悬空提交与不可达对象（amend/reset 残留常在这里）。
4. mode=show 用 commit:path 取回被删文件内容；loose objects 也可用 tool=python_exec 逐个解压解析。

## 命中判据
- 历史对象内容含唯一 flag{...}。
- 负判据：对象截断或仓库损坏；写 ruled_out 已试恢复方式。

## 何时换路
- 仓库里还有图片/隐写转 forensics.stego.image；提交信息是编码文本转 crypto.encoding。

## 踩坑
- 只看工作树会漏掉被删历史；先 log 再 fsck 双路都走。
- show 的 commit:path 冒号前是提交、冒号后是文件路径，别写反。

## 来源/边界
- 内部编写；依赖注册工具 git_history（镜像需含 git）。
