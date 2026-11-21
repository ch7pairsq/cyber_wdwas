# web.java.deser

## 触发
- Java 栈特征（报错栈 java.*、JNDI/LDAP/RMI 字样、对象流 rO0Ab 头）或题名含 lookup/jndi。

## 最小动作
1. tool=arena_http 摸提交点：是否接收 Java 序列化流/base64 对象、LDAP URL 参数。
2. 反序列化确认：rO0Ab 头；可控类名时先探 URLDNS 类验证解析与出网。
3. JNDI 注入指向可控 LDAP/RMI；受限网段通常禁外连，改打本地依赖 gadget 链。
4. tool=python_exec 按依赖版本手工构造可达链或改走表达式注入（SpEL/OGNL 类）路线。

## 命中判据
- 服务端执行结果含唯一 flag{...} 或回连探测命中。
- 负判据：无可用 gadget 依赖且外连被禁；写 ruled_out 依赖清单。

## 何时换路
- 有模板渲染点转 web.ssti；参数注入数据库转 web.sqlinjection。

## 踩坑
- 大而全 payload 易触发防护，按依赖版本裁剪链路。
- 可用链直接 tool=ysoserial 出 payload（输出 base64）；URLDNS 先验证解析。
- 无出网时 DNS 探测也会失败，先判网段策略再下结论。

## 来源/边界
- 内部编写；不覆盖需本地 Java 环境的复杂构造。
