"""模型 Key 的可选落盘缓存（2026-11-18 新增，默认关闭）。

背景：环境变量在进程重启与容器重启（docker restart）时都会保留，不需要落盘；
只有"平台重建容器且不重注入环境变量、同时挂载了持久卷"的场景才需要落盘兜底。
启用条件：config model.key_cache_file 或环境变量 CTF_MODEL_KEY_CACHE 指向可写路径；
两条通道共用 BAIZHI_API_KEY 注入名，缓存的是"当前通道的 Key"，切换通道后首次
启动会用环境变量值覆盖缓存。

纪律：缓存文件 0600、原子写入；不进 SQLite／证据／报告／日志；写入失败只警告
不阻断（缓存是保险，不是硬依赖）；本轮运行正常结束时清理，异常中断时保留并在
下次启动被环境变量值覆盖。
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Tuple

DEFAULT_KEY_ENV = "BAIZHI_API_KEY"
CACHE_PATH_ENV = "CTF_MODEL_KEY_CACHE"

_last_cache_path: Optional[str] = None


def cache_path(model_cfg) -> Optional[str]:
    """解析缓存路径；未启用返回 None。配置项优先于环境变量。"""
    cfg = model_cfg if isinstance(model_cfg, dict) else {}
    path = cfg.get("key_cache_file")
    if isinstance(path, str) and path.strip():
        return path
    env = os.environ.get(CACHE_PATH_ENV)
    if env and env.strip():
        return env
    return None


def resolve_model_key(model_cfg) -> Tuple[str, str]:
    """返回 (key, source)；source 为 'env' 或 'cache'。两者都拿不到时 SystemExit。"""
    cfg = model_cfg if isinstance(model_cfg, dict) else {}
    env_name = cfg.get("api_key_env") or DEFAULT_KEY_ENV
    key = os.environ.get(env_name)
    if key:
        return key, "env"
    path = cache_path(cfg)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cached = fh.read().strip()
        except OSError:
            cached = ""
        if cached:
            return cached, "cache"
    message = "model key missing: export {} before --mode llm".format(env_name)
    if path:
        message += " (key cache unavailable: {})".format(path)
    raise SystemExit(message)


def sync_key_cache(model_cfg, key: str) -> None:
    """启用缓存时把环境变量来源的 Key 原子写入缓存文件；失败只警告不阻断。"""
    global _last_cache_path
    path = cache_path(model_cfg)
    if not path or not key:
        return
    _last_cache_path = path
    tmp = path + ".tmp"
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(key)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        print("key cache unavailable ({}); continuing with environment key".format(
            type(exc).__name__), file=sys.stderr)


def clear_key_cache(path: Optional[str] = None) -> None:
    """清理本轮缓存文件；默认清理本轮 sync 写入的路径。缺文件不报错。"""
    global _last_cache_path
    target = path or _last_cache_path
    _last_cache_path = None
    if not target:
        return
    try:
        os.unlink(target)
    except OSError:
        pass
