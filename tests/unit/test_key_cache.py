"""模型 Key 落盘缓存的离线回归（2026-11-18 用户决定新增，默认关闭）。

验证：环境变量优先、缓存兜底、权限 0600、原子写入、清理行为与默认关闭。
"""
import os
import stat
import sys
import tempfile
from unittest.mock import patch

from ctf_ext.llm import keycache


def expect_system_exit(fn, fragment):
    try:
        fn()
    except SystemExit as error:
        assert fragment in str(error), str(error)
        return
    raise AssertionError("expected SystemExit containing " + fragment)


def test_env_key_writes_cache_with_strict_permissions():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "model.key")
        model_cfg = {"key_cache_file": path}
        with patch.dict(os.environ, {"BAIZHI_API_KEY": "env-key"}, clear=True):
            key, source = keycache.resolve_model_key(model_cfg)
            assert source == "env" and key == "env-key"
            keycache.sync_key_cache(model_cfg, key)
        assert os.path.isfile(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)
        with open(path, "r", encoding="utf-8") as fh:
            assert fh.read() == "env-key"


def test_env_missing_falls_back_to_cache():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "model.key")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("cached-key\n")
        with patch.dict(os.environ, {}, clear=True):
            key, source = keycache.resolve_model_key({"key_cache_file": path})
            assert source == "cache" and key == "cached-key"


def test_missing_everywhere_fails_closed_with_env_name():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "absent.key")
        with patch.dict(os.environ, {}, clear=True):
            expect_system_exit(lambda: keycache.resolve_model_key(
                {"key_cache_file": path, "api_key_env": "BAIZHI_API_KEY"}), "BAIZHI_API_KEY")
            expect_system_exit(lambda: keycache.resolve_model_key({}), "BAIZHI_API_KEY")


def test_cache_disabled_keeps_env_only_behaviour():
    with patch.dict(os.environ, {}, clear=True):
        assert keycache.cache_path({}) is None
        assert keycache.cache_path(None) is None
        expect_system_exit(lambda: keycache.resolve_model_key({}), "BAIZHI_API_KEY")


def test_cache_path_from_environment_variable():
    with patch.dict(os.environ, {"CTF_MODEL_KEY_CACHE": "/tmp/from-env.key"}, clear=True):
        assert keycache.cache_path({}) == "/tmp/from-env.key"
        assert keycache.cache_path({"key_cache_file": "/tmp/from-config.key"}) == "/tmp/from-config.key"


def test_cache_write_failure_warns_without_raising():
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o500)  # 只读目录：写入必须失败
        try:
            with patch.dict(os.environ, {}, clear=True):
                keycache.sync_key_cache({"key_cache_file": os.path.join(tmp, "model.key")}, "k")
        finally:
            os.chmod(tmp, 0o700)


def test_clear_key_cache_removes_file_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "model.key")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("k")
        keycache.clear_key_cache(path)
        assert not os.path.exists(path)
        keycache.clear_key_cache(path)  # 重复清理不报错
        keycache.clear_key_cache()      # 未记录路径时静默返回


def test_sync_without_path_records_nothing():
    keycache.clear_key_cache()  # 清掉其他用例可能留下的记录路径
    with tempfile.TemporaryDirectory() as tmp:
        keycache.sync_key_cache({}, "k")
        assert keycache._last_cache_path is None
        path = os.path.join(tmp, "model.key")
        with patch.dict(os.environ, {"CTF_MODEL_KEY_CACHE": path}, clear=True):
            keycache.sync_key_cache({}, "env-key")
            assert keycache._last_cache_path == path
        assert os.path.isfile(path)
