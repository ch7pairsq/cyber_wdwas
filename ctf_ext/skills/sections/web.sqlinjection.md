# web.sqlinjection

## 触发
- 查询参数/登录框/搜索接口，回显含 SQL 报错，或行为随引号变化。

## 最小动作
1. tool=arena_http 读首页与源码泄露路径（robots.txt、/.git/、备份文件），确认后端类型与参数点。
2. tool=python_exec 探测：单引号报错 → 布尔差异（and 1=1 / and 1=2）→ 时间盲注，按回显选路。
3. 有回显：联合查询 information_schema 拿表列；无回显：逐字符提取；定位 flag 列后导出。

## 命中判据
- 从服务端读出唯一 flag{...}。
- 负判据：确认注入但被拦截；写 ruled_out 已试编码（大小写、注释、内联）。

## 何时换路
- SQLite 换 sqlite_master；报错含模板代码转 web.ssti；鉴权弱点转 web.jwt。

## 踩坑
- 附件占位 flag 直接提交必拒；以服务端真实读出为准。
- 注入点可能在 POST/JSON/Cookie，不只看 GET 参数。
- 盲注脚本设超时与重试，避免限流造成假阴性。

## 来源/边界
- 内部编写；不涵盖 NoSQL 与 ORM 绕过。
