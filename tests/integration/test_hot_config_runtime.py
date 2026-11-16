"""热更新接线：配置变更生效、非法配置不生效、快照落库（§13）。"""
import json
import os
import sqlite3
import tempfile

from ctf_ext.__main__ import build_runtime


def _config():
    return {
        "concurrency": {"max_workers": 4, "max_project_workers": 2, "max_running_projects": 2},
        "runtime": {"worker_lease_seconds": 120, "heartbeat_seconds": 15},
        "budget": {"per_intent_seconds": 600, "per_challenge_seconds": 900},
        "download": {"max_single_bytes": 0, "max_total_bytes": 0},
        "policy": {"authorized_scope": [], "command_allowlist": []},
        "model": {"provider": "deepseek"},
        "storage": {"db_path": "unused.db"},
        "harness": {"protocol": "mock", "official_sdk_ready": False, "competition_locked": False},
    }


def _write(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)


def test_hot_reload_updates_scheduler_and_writes_snapshots():
    tmp = tempfile.mkdtemp(prefix="hotit-")
    db = os.path.join(tmp, "p.db")
    path = os.path.join(tmp, "config.json")
    cfg = _config()
    _write(path, cfg)
    orch = build_runtime(db, os.path.join(tmp, "ev"), config_path=path)
    assert orch._scheduler._max_project_workers == 2
    assert orch._scheduler._max_running_projects == 2

    cfg["concurrency"]["max_project_workers"] = 3
    cfg["concurrency"]["max_running_projects"] = 4
    _write(path, cfg)
    assert orch.reload_config_if_changed() is True
    assert orch._scheduler._max_project_workers == 3
    assert orch._scheduler._max_running_projects == 4

    conn = sqlite3.connect(db)
    snapshots = conn.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0]
    events = [r[0] for r in conn.execute("SELECT event_type FROM events")]
    assert snapshots == 2
    assert "config_reloaded" in events


def test_official_protocol_is_refused_without_sdk():
    tmp = tempfile.mkdtemp(prefix="hotit-")
    path = os.path.join(tmp, "config.json")
    cfg = _config()
    cfg["harness"] = {"protocol": "official", "official_sdk_ready": False,
                      "competition_locked": True}
    _write(path, cfg)
    from ctf_ext.config import ConfigError
    try:
        build_runtime(os.path.join(tmp, "p.db"), os.path.join(tmp, "ev"), config_path=path)
        raise AssertionError("expected config rejection for official protocol")
    except (ConfigError, SystemExit) as exc:
        assert "official" in str(exc)
