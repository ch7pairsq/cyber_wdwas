"""配置层：§13 别名、Fail Closed 占位、校验与热更新快照（§13）。"""
import json
import os
import sqlite3
import tempfile

from ctf_ext.config import HotConfig, config_hash, normalize_config, validate_config
from ctf_ext.domain.migrate import migrate


def placeholder(name):
    return "$" + "{" + name + "}"


def _base():
    return {
        "concurrency": {"max_workers": 4, "max_project_workers": 2, "max_running_projects": 3},
        "runtime": {"worker_lease_seconds": 180, "heartbeat_seconds": 15},
        "budget": {"per_intent_seconds": 600, "per_challenge_seconds": 900},
        "download": {"max_single_bytes": 0, "max_total_bytes": 0},
        "policy": {"authorized_scope": [], "command_allowlist": []},
        "model": {"provider": "deepseek"},
        "storage": {"db_path": "data/practice.db"},
        "harness": {"protocol": "mock", "official_sdk_ready": False, "competition_locked": False},
    }


def test_section_13_aliases_map_to_canonical():
    raw = {
        "runtime": {"max_workers": 6, "max_project_workers": 2, "worker_lease_seconds": 120},
        "budgets": {"per_intent_seconds": 300, "per_challenge_seconds": 600,
                    "max_model_tokens_per_intent": 50000},
        "downloads": {"max_single_download_bytes": 1024, "max_total_download_bytes": 4096},
        "app": {"mode": "competition"},
    }
    cfg = normalize_config(raw)
    assert cfg["concurrency"]["max_workers"] == 6
    assert cfg["concurrency"]["max_project_workers"] == 2
    assert cfg["runtime"]["worker_lease_seconds"] == 120
    assert cfg["budget"]["per_intent_seconds"] == 300
    assert cfg["budget"]["max_model_tokens_per_intent"] == 50000
    assert cfg["download"]["max_single_bytes"] == 1024
    assert cfg["download"]["max_total_bytes"] == 4096
    assert cfg["app"]["mode"] == "competition"


def test_canonical_name_wins_over_alias():
    cfg = normalize_config({"concurrency": {"max_workers": 4}, "runtime": {"max_workers": 8}})
    assert cfg["concurrency"]["max_workers"] == 4


def test_env_placeholder_fail_closed():
    os.environ.pop("CTF_TEST_UNSET_LIMIT", None)
    cfg = normalize_config({"downloads": {"max_single_download_bytes": placeholder("CTF_TEST_UNSET_LIMIT")}})
    assert cfg["download"]["max_single_bytes"] is None  # 未定义 → 后续按 0 处理
    os.environ["CTF_TEST_SET_LIMIT"] = "2048"
    try:
        cfg = normalize_config({"downloads": {"max_single_download_bytes": placeholder("CTF_TEST_SET_LIMIT")}})
        assert cfg["download"]["max_single_bytes"] == "2048"
    finally:
        os.environ.pop("CTF_TEST_SET_LIMIT", None)


def test_validation_catches_hard_caps():
    good = _base()
    assert validate_config(good) == []
    for mutate, needle in (
        (lambda c: c["concurrency"].update(max_workers=9), "hard cap"),
        (lambda c: c["concurrency"].update(max_workers=2, max_project_workers=3), "max_project_workers"),
        (lambda c: c["runtime"].update(heartbeat_seconds=200), "heartbeat_seconds"),
        (lambda c: c["budget"].update(per_intent_seconds=1000), "per_intent_seconds"),
        (lambda c: c["storage"].pop("db_path"), "db_path"),
        (lambda c: c["harness"].update(protocol="official"), "official_sdk_ready"),
        (lambda c: c["harness"].pop("competition_locked"), "competition_locked"),
        (lambda c: c["download"].update(max_total_bytes=-1), "max_total_bytes"),
    ):
        cfg = _base()
        mutate(cfg)
        errors = validate_config(cfg)
        assert any(needle in e for e in errors), (needle, errors)


def _write(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)


def test_hot_reload_keeps_invalid_config_and_snapshots():
    tmp = tempfile.mkdtemp(prefix="hotcfg-")
    path = os.path.join(tmp, "config.json")
    cfg = _base()
    _write(path, cfg)
    hot = HotConfig(path)
    assert hot.version == 1
    assert hot.reload_if_changed() is False

    cfg["concurrency"]["max_workers"] = 6
    cfg["concurrency"]["max_project_workers"] = 3
    _write(path, cfg)
    assert hot.reload_if_changed() is True
    assert hot.version == 2 and hot.config["concurrency"]["max_workers"] == 6

    bad = dict(cfg)
    bad["concurrency"] = {"max_workers": 99, "max_project_workers": 2}
    _write(path, bad)
    assert hot.reload_if_changed() is False  # 非法配置不生效
    assert hot.config["concurrency"]["max_workers"] == 6
    assert "hard cap" in hot.last_error

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    hot.snapshot(conn, source="test")
    row = conn.execute("SELECT config_hash, source FROM config_snapshots").fetchone()
    assert row[0] == config_hash(hot.config) and row[1] == "test"
