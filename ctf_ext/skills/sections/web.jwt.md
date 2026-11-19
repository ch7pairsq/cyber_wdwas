# web.jwt

## 触发
- 鉴权用 JWT（三段 base64url，头部含 alg）；题目要求越权访问 admin 或伪造身份。

## 最小动作
1. tool=python_exec 解码头部与载荷（base64url + JSON），看 alg、kid、exp、角色字段。
2. alg=none：去签名直接重放；HS256：用题面线索做弱口令表本地 HMAC 爆破密钥。
3. 拿到密钥后 tool=python_exec 重签载荷（role/uid 变更），tool=arena_http 带新 token 打目标接口读 flag。
4. RS256→HS256 混淆：用公钥当 HMAC 密钥签名。

## 命中判据
- 新 token 访问返回内容含唯一 flag{...}。
- 负判据：签名校验严格且密钥强；写 ruled_out 后转源码审计。

## 何时换路
- token 无签名段可能是自定义编码，转 crypto.encoding；接口存在模板渲染转 web.ssti。

## 踩坑
- kid 参数可注入时注意路径穿越与空值变体。
- 爆破字典用题面线索（用户名/题名/日期），不要全表盲跑。

## 来源/边界
- 内部编写；不覆盖 JWE 与非对称实现漏洞。
