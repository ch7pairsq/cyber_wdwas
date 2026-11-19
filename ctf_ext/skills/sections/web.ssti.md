# web.ssti

## 触发
- 页面把用户输入渲染进模板；回显计算结果或报错栈指向模板引擎（Jinja2/Mako/Twig/ERB）。

## 最小动作
1. tool=arena_http 对可控参数注入探测串：{{7*7}}、${7*7}、<%= 7*7 %>，回显 49 即命中并区分引擎。
2. tool=python_exec 按引擎构造 payload：Python 系走类继承链定位内置模块；Java/Twig 系按各自语法树构造。
3. 目标两步走：先读服务端文件（/flag、/proc/self/environ），再考虑命令执行。

## 命中判据
- 读到唯一 flag{...} 或命令输出含 flag{...}。
- 负判据：探测串原样输出说明未进模板；写 ruled_out 已试的定界符。

## 何时换路
- 输入点在 SQL 语句转 web.sqlinjection；可控 URL 转发转 web.ssrf。

## 踩坑
- {{7*7}} 被过滤时试双写与内层定界符变体，不要在 payload 里嵌套未转义引号。
- 多引擎按报错特征区分，盲试 payload 浪费轮次。

## 来源/边界
- 内部编写；只覆盖常见模板引擎首查链。
