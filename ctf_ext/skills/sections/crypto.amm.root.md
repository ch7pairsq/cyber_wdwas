# crypto.amm.root

## 触发
- 模开方/高次剩余类题：给定 p 与 x 求 x^(1/r) mod p（Tonelli-Shanks、AMM、sqrt163 等 r 次根题）、模 p^k 开方、离散对数（dlog/BSGS）、二次剩余判定。

## 最小动作
1. tool=python_exec 直接跑（工具用系统 Python /usr/bin/python3，已预装 gmpy2 与 sympy，不要装 sage、不要 pip install）：
   python: import gmpy2; from sympy.ntheory.residue_ntheory import nthroot_mod, sqrt_mod, discrete_log
2. 素数模 r 次根：roots = nthroot_mod(a, r, p, all_roots=True)（内部 Tonelli-Shanks/AMM）。模 p 的 r 次根共有 r 个，**全部**逐个带入下游解密，只有一个能还原出可读明文/flag。
3. 模 p^k（k>1）：先对 mod p 求一个根 r0，再做 Hensel 提升：r_{i+1} = (r_i - (r_i^r - a) * inv(r * r_i^(r-1), p^(i+1))) % p^(i+1)，循环到 p^k；gmpy2.invert 求逆。
4. 二次剩余判定：gmpy2.legendre(a, p) == 1 才有根；gmpy2.iroot 做大整数开根（非模意义）；gmpy2.invert 免手写扩展欧几里得。
5. 离散对数（群阶不大，如 < 2^40）：sympy discrete_log(p, h, g)；阶大时先试小群/光滑阶分解，或手写 BSGS（gmpy2 加速）。

## 命中判据
- 验证 pow(root, r, p) == a % p；若都要试，全部根带入下游，只有一条链产出可读明文/flag{...}。
- 负判据：legendre(a,p) != 1 或无根时 nthroot_mod 返回 None——写 ruled_out，转因式分解方向。

## 何时换路
- 模数非素数且分解困难：转 crypto.rsa.variants（费马/部分位）；有格结构转 crypto.lattice.lll。

## 踩坑
- nthroot_mod 只支持素数模；p^k 必须自己 Hensel 提升，别直接套素数模函数。
- sympy 大数慢（秒级到分钟级）：先用 gmpy2 判剩余/做快速幂筛选，再 sympy 求根；python_exec 限额 CPU 110s/内存 512MB，大指数连乘用 pow(a, e, n)。
- 二次剩余有两个根（±r）：两个都要试；r 次根有 r 个（含复数解在模意义下），全部遍历。

