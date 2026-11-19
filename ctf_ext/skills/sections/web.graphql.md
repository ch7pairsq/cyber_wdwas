# web.graphql

## 触发
- 接口是 GraphQL（/graphql、POST body 为 query），或响应含 {"data":...}/"errors" 结构。

## 最小动作
1. tool=arena_http 发 introspection 查询（__schema 类型与字段），未禁用即拿全量 schema。
2. tool=python_exec 按 schema 找敏感对象与越权点（flag、admin、user(id)），构造嵌套查询逐字段取数。
3. introspection 被禁：用字段猜测与建议报错（Did you mean）补全 schema；再探 Mutation 写接口。
4. 用别名与批量在单请求里做多组注入/越权探测，提效率。

## 命中判据
- 响应 data 含唯一 flag{...}。
- 负判据：仅开放白名单查询且鉴权完整；写 ruled_out 已枚举类型。

## 何时换路
- 变量拼进 SQL 转 web.sqlinjection；返回经模板渲染转 web.ssti。

## 踩坑
- GET 与 POST、json 与 form 两种入口行为可能不同，都试。
- 深嵌套可能触发防护，分步取数控制深度。

## 来源/边界
- 内部编写；不覆盖 subscriptions 与上传变异。
