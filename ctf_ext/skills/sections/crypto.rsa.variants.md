# crypto.rsa.variants

## 触发
- RSA 变体题：维纳/费马类提示、给定部分素数位、n 为多项式值、e 与 phi 关系、批量密文。

## 最小动作
1. tool=python_exec 列参数清单：n/e/c 与附加量（dp、p 高位、多素数、关系式）。
2. 按特征选攻击：素数相近用费马；d 小用维纳；部分位已知转格建模（crypto.lattice.lll）。
3. 变体结构（p²q、自逆指数等）先推数学关系再解，tool=python_exec 验证解满足原式。
4. 明文按题面编码链还原。

## 命中判据
- 解码结果为唯一 flag{...}。
- 负判据：参数均安全；写 ruled_out 已试攻击与依据。

## 何时换路
- 共模转 crypto.rsa.common_modulus；小 e 转 crypto.rsa.small_e；有格结构转 crypto.lattice.lll。

## 踩坑
- 先验证已知量自洽（关系式成立）再套攻击，排除题目自定义变换。
- 批量密文共用素数等跨题线索要横向对比。

## 来源/边界
- 内部编写；不覆盖大参数通用分解。
