"""SkillLoader.select 打分规则回归（2026-11-20 收紧）。

教训来自正式赛：static_file_server（Python web 题）被配上 web.php 技能、
weak-password（hash 破解题）被配上 forensics.archive_layers——根因是子串
匹配、碎片词、重复计分与低门槛。本文件同时锁定收紧后的通用规则与两道
真题的回归结果。
"""
from ctf_ext.skills.loader import SkillLoader

# 正式赛两道题的真实查询（load_for 的 query = name + description 的有效片段）
_WEAK_PASSWORD_QUERY = (
    "ctftiny2025__weak-password Can you crack Aaron's password hash? He seems "
    "to like simple passwords. I'm sure he'll use his name and birthday in it. "
    "Hint: Aaron writes important dates as YYYYMMDD. Once you crack the "
    "password, prepend it with flag{{ and append it with }} to submit. "
    "Hash: 7f4986da7d7b52fa81f98"
)
_STATIC_FILE_QUERY = (
    "downunderctf2023__static_file_server solve this CTF static file server "
    "simple Python app that lets you view some files on the server joseph "
    "Dockerfile app.py README.md files/not_the_flag.txt files/ductf.png"
)


def _loader():
    return SkillLoader()


def test_substring_terms_do_not_match():
    # "file" 不得命中 zipfile：词边界匹配。
    hits = _loader().select(domain="ctf", query="Dockerfile file zipfile")
    assert all("zipfile" not in str(m["name"]) for m in hits)
    # "trust" 内部的 "rust" 子串不得命中 rust 卡（整词 rust 命中是合法的，
    # 由 test_alias_whole_word_hits_rust 锁定）。
    assert all("reverse.rust" not in str(m["name"])
               for m in _loader().select(domain="ctf", query="Dockerfile trust"))


def test_short_fragments_excluded():
    # 2 字符碎片词（py、md）不参与打分。
    assert _loader().select(domain="ctf", query="py md io") == []


def test_duplicate_terms_score_once():
    # 重复词只计一次：password 出现 3 次不再把 archive_layers 顶上名。
    hits = _loader().select(domain="ctf",
                            query="password password password weak")
    assert all("archive_layers" not in str(m["name"]) for m in hits)


def test_single_keyword_hit_not_enough():
    # 单关键词 3 分低于 5 分门槛，不入围（宁缺毋滥）。
    assert _loader().select(domain="ctf", query="qr comment") == []


def test_two_distinct_term_hits_pass():
    # 两个不同实词命中（3+3=6）可以入围。
    hits = _loader().select(domain="ctf", query="zip archive comment")
    assert any("forensics.zip" in str(m["name"]) for m in hits)


def test_domain_exact_match_always_kept():
    # domain 精确命中 +6，即使只有一个实词也入围。
    hits = _loader().select(domain="web", query="php")
    assert any(str(m["name"]) == "web.php" for m in hits)


def test_name_and_keyword_same_term_passes():
    # rsa：name 与 keyword 同词各命中一次（2+3=5），达到门槛。
    hits = _loader().select(domain="crypto", query="rsa modulus")
    assert any(str(m["name"]) == "crypto.rsa.small_e" for m in hits)


def test_real_weak_password_regression():
    # hash 破解题：archive_layers（压缩包密码）与 php 均不相关，不得入围。
    hits = _loader().select(domain="ctf", query=_WEAK_PASSWORD_QUERY)
    names = [str(m["name"]) for m in hits]
    assert "forensics.archive_layers" not in names
    assert "web.php" not in names


def test_real_static_file_server_regression():
    # Python web 题：php 技能与 zipfile 均不得再入围。
    hits = _loader().select(domain="ctf", query=_STATIC_FILE_QUERY)
    names = [str(m["name"]) for m in hits]
    assert "web.php" not in names
    assert "misc.pt.zipfile" not in names


def test_alias_hits_glued_chinese_boundary():
    # 中文题面把英文专名与中文粘连（"Shiro反序列化"）：alias 边缘只对
    # ASCII 侧生效，粘连不失效；+7 高于域分，跨域细节卡必须排第一。
    hits = _loader().select(query="Shiro反序列化漏洞", limit=2)
    assert hits and str(hits[0]["name"]) == "web.java.deser"


def test_alias_ascii_boundary_no_false_hit():
    # "javascript" 不得命中 java 类卡；"self" 不得命中 "elf"。
    hits = _loader().select(query="javascript 前端混淆", limit=5)
    assert all("java" not in str(m["name"]) or str(m["name"]) == "web.java.deser" is False
               for m in hits if "java.deser" in str(m["name"])) is True or all(
        "web.java" not in str(m["name"]) for m in hits)
    assert all("elf" not in str(m["name"]).split(".", 1)[1]
               for m in _loader().select(query="self 变量", limit=5))


def test_generic_alias_does_not_evict_native_domain():
    # 泛词回归（2026-11-22 审计）：纯中文 misc 题提"编码"不得把
    # crypto.encoding（跨域）顶到本域卡之前——泛词已从 aliases 删除。
    hits = _loader().select(
        domain="杂项",
        query="题目给出一段特殊文字，需要还原成原始内容。（原题描述：被处理过的数据）编码",
        limit=3)
    names = [str(m["name"]) for m in hits]
    assert "crypto.encoding" not in names
    assert names and str(hits[0]["name"]).startswith("misc.")


def test_generic_alias_does_not_evict_pwn_slot():
    # 泛词回归（2026-11-22 审计）：pwn 纯中文题提"反汇编"不得让
    # reverse.elf.static 占掉 pwn 本域卡的槽位。
    hits = _loader().select(
        domain="二进制",
        query="程序存在栈溢出，需要覆盖返回地址，注意反汇编查看函数逻辑。",
        limit=3)
    names = [str(m["name"]) for m in hits]
    assert "reverse.elf.static" not in names
    assert names and str(hits[0]["name"]).startswith("pwn.")


def test_alias_whole_word_hits_rust():
    # 整词 "rust" 合法命中 reverse.rust（alias 命中），与子串防误伤互补。
    hits = _loader().select(domain="ctf", query="Dockerfile trust rust", limit=2)
    assert any(str(m["name"]) == "reverse.rust" for m in hits)
