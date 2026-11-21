# web.pathtraversal

## 触发
- 下载/预览/静态接口带文件名参数（?f=、?page=、/static/），响应像文件内容。

## 最小动作
1. tool=arena_http 逐层试探：../、..%2f、%2e%2e/、....//、绝对路径，读 /etc/passwd 确认可达。
2. 命中后读目标：/flag、/flag.txt、/proc/self/environ、/proc/self/cmdline、应用源码定位 flag 路径。
3. 被过滤时 tool=python_exec 生成变体：双写、混合编码、尾部截断、伪协议包装。

## 命中判据
- 读到唯一 flag{...}，或从源码确认 flag 路径后继续读。
- 负判据：接口只允许白名单后缀；写 ruled_out 已试编码清单。

## 何时换路
- 伪协议读到模板源码转 web.ssti；参数直拼 SQL 转 web.sqlinjection。

## 踩坑
- ../ 剥离层数不定，逐级加；解压接口包内路径同样可穿越。
- 二进制响应看 Content-Type，别按纯文本解析。
- nginx 配置题：找捕获组直塞重写目标的写法（try_files/rewrite）；/static/../ 与 /%2e%2e 分开测（规范化与解码是独立开关）；顺读 /entrypoint.sh /flag.txt。

## 来源/边界
- 内部编写；不覆盖容器逃逸类文件写入。
