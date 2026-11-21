# web.xxe

## 触发
- 接口接受 XML（Content-Type text/xml、SOAP、docx/svg 上传后被解析）。
- 回显含文件内容，或报错带路径/实体名。

## 最小动作
1. tool=arena_http 发标准实体探针：DOCTYPE 内定义 SYSTEM 实体指向 file:///etc/passwd 并在正文引用。
2. 无回显：改用报错注入（实体内容拼进解析错误）或参数实体外带；先探支持哪些协议封装。
3. tool=python_exec 解析返回、定位 flag 路径后逐个读取；docx/svg 把 payload 放进合法文档结构再传。

## 命中判据
- 读出唯一 flag{...}。
- 负判据：解析器禁外部实体；写 ruled_out 并转上传线。

## 何时换路
- 解析点在上传转 web.upload；返回 JSON 结构转 web.sqlinjection。

## 踩坑
- PHP/Java 支持的封装协议不同，先探协议再打路径。
- 实体引用要同时改 DOCTYPE 与正文两处；只改一处会静默失败。

## 来源/边界
- 内部编写；不涵盖 XXE 到 RCE 的协议利用。
