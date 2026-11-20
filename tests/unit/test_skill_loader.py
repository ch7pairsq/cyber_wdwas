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
    # "file" 不得命中 zipfile、"rust" 不得命中 trust：词边界匹配。
    hits = _loader().select(domain="ctf", query="Dockerfile file zipfile trust rust")
    assert all("zipfile" not in str(m["name"]) for m in hits)
    assert all("rust" not in str(m["name"]) for m in hits)


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
