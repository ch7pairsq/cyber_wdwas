# web.cgi.rce

## 触发
- 目标暴露 /cgi-bin/ 或 .cgi/.pl 脚本；参数回显带命令结果迹象；Apache+CGI 老架构。

## 最小动作
1. tool=arena_http 探测：脚本是否存在、参数如何进命令（回显/时间差/报错）。
2. 命令注入按序试：分隔符（;|&&）→ 输出重定向到可读路径 → 盲注用时间侧信道；tool=python_exec 批量变体与基线比对。
3. Shellshock：bash CGI 用恶意 Header（() { :;}; + 命令）触发；tool=arena_http 发畸形 Header 观察响应差。
4. 拿到执行读 flag；只读场景（源码泄露）转信息收集并记 ruled_out。

## 命中判据
- 命令执行返回唯一 flag{...} 或可复核的执行证据。
- 负判据：脚本仅回显不解析；写 ruled_out 记录已试分隔符与 Header。

## 何时换路
- 注入点在 SQL 层转 web.sqlinjection；文件包含转 web.pathtraversal。

## 踩坑
- 时间盲注先测脚本基线耗时再对比，区分"脚本慢"与"命令慢"。
- Shellshock 先确认后端是 bash 再打变体，别对 dash/ash 空耗。

## 来源/边界
- 内部编写；不涵盖 CGI 程序自身 0day。
