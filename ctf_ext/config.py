"""配置：加载、§13 别名归一、环境变量展开、校验、热更新与快照（§13）。

- 兼容设计文档 §13 示例键名（runtime.* / budgets.* / downloads.*）与本项目规范名；
- 环境变量占位未定义时下载限额按 0 处理（Fail Closed）；
- 热更新只替换通过校验的新配置，非法配置保留旧值并记录错误。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple


class ConfigError(Exception):
    pass


# 规范名（本项目内部）：concurrency / runtime / budget / download / policy / model / storage / harness
ALIAS_SECTIONS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "runtime": {
        "max_workers": ("concurrency", "max_workers"),
        "max_project_workers": ("concurrency", "max_project_workers"),
        "per_challenge_workers": ("concurrency", "max_project_workers"),
        "planner_parallelism": ("concurrency", "planner_parallelism"),
        "worker_lease_seconds": ("runtime", "worker_lease_seconds"),
        "heartbeat_seconds": ("runtime", "heartbeat_seconds"),
        "intent_poll_seconds": ("runtime", "intent_poll_seconds"),
    },
    "budgets": {
        "per_intent_seconds": ("budget", "per_intent_seconds"),
        "per_challenge_seconds": ("budget", "per_challenge_seconds"),
        "max_tool_calls_per_intent": ("budget", "max_tool_calls_per_intent"),
        "max_model_tokens_per_intent": ("budget", "max_model_tokens_per_intent"),
        "max_evidence_preview_bytes": ("budget", "max_evidence_preview_bytes"),
    },
    "downloads": {
        "max_single_download_bytes": ("download", "max_single_bytes"),
        "max_total_download_bytes": ("download", "max_total_bytes"),
        "allow_http": ("download", "allow_http"),
        "allow_https": ("download", "allow_https"),
    },
    "app": {
        "mode": ("app", "mode"),
        "run_deadline_seconds": ("app", "run_deadline_seconds"),
        "shutdown_reserve_seconds": ("app", "shutdown_reserve_seconds"),
    },
}

_ENV_RE = re.compile(r"[$][{]([A-Z0-9_]+)[}]")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if not isinstance(value, str):
        return value
    match = _ENV_RE.fullmatch(value.strip())
    if match:  # 整值替换：未定义 → None，由校验/默认值决定 Fail Closed
        return os.environ.get(match.group(1))
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把 §13 示例键名映射到规范名；规范名优先，不清除未知键（便于审计）。"""
    cfg = _expand_env(raw or {})
    for alias_section, mapping in ALIAS_SECTIONS.items():
        section = cfg.get(alias_section)
        if not isinstance(section, dict):
            continue
        for alias_key, (target_section, target_key) in mapping.items():
            if alias_key not in section:
                continue
            target = cfg.setdefault(target_section, {})
            if not isinstance(target, dict):
                continue
            target.setdefault(target_key, section[alias_key])  # 规范名优先
    return cfg


def _num(cfg: Dict[str, Any], section: str, key: str, default=0) -> float:
    value = (cfg.get(section) or {}).get(key, default)
    if value is None or value == "":
        return 0  # 未配置 → 0（下载限额因此 Fail Closed）
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError("{}.{} must be numeric, got {!r}".format(section, key, value))


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    workers = int(_num(cfg, "concurrency", "max_workers", 0))
    project = int(_num(cfg, "concurrency", "max_project_workers", 0))
    if workers < 1:
        errors.append("concurrency.max_workers must be >= 1")
    if workers > 8:
        errors.append("concurrency.max_workers must be <= 8 (hard cap, §13)")
    if project < 1:
        errors.append("concurrency.max_project_workers must be >= 1")
    if project > workers:
        errors.append("concurrency.max_project_workers must be <= max_workers")

    lease = _num(cfg, "runtime", "worker_lease_seconds", 180)
    heartbeat = _num(cfg, "runtime", "heartbeat_seconds", 15)
    if lease <= 0:
        errors.append("runtime.worker_lease_seconds must be > 0")
    if heartbeat <= 0 or heartbeat >= lease:
        errors.append("runtime.heartbeat_seconds must be in (0, worker_lease_seconds)")

    per_intent = _num(cfg, "budget", "per_intent_seconds", 0)
    per_challenge = _num(cfg, "budget", "per_challenge_seconds", 0)
    if per_intent < 0 or per_challenge < 0:
        errors.append("budget.* must be >= 0")
    if per_intent and per_challenge and per_intent > per_challenge:
        errors.append("budget.per_intent_seconds must be <= per_challenge_seconds")

    for key in ("max_single_bytes", "max_total_bytes"):
        if _num(cfg, "download", key, 0) < 0:
            errors.append("download.{} must be >= 0 (0 = fail closed)".format(key))

    harness = cfg.get("harness") or {}
    if harness.get("protocol") not in ("mock", "official"):
        errors.append("harness.protocol must be 'mock' or 'official'")
    if harness.get("protocol") == "official" and not harness.get("official_sdk_ready"):
        errors.append("harness.protocol=official requires official_sdk_ready=true (SDK not yet obtained)")
    if "competition_locked" not in harness:
        errors.append("harness.competition_locked must be set explicitly (true for competition)")
    if not (cfg.get("storage") or {}).get("db_path"):
        errors.append("storage.db_path is required")
    return errors


def load_yaml(path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ConfigError("PyYAML is required to read {}: pip install pyyaml".format(path)) from exc
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_json(path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_file(path) -> Dict[str, Any]:
    path = str(path)
    return load_json(path) if path.endswith(".json") else load_yaml(path)


def load_config(*paths) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for path in paths:
        merged = deep_merge(merged, load_file(path))
    return normalize_config(merged)


def config_hash(cfg: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class HotConfig:
    """受控配置快照热更新：只接受通过校验的新版本，并写入 config_snapshots。"""

    def __init__(self, *paths):
        self._paths = [str(p) for p in paths]
        self.config = load_config(*self._paths)
        errors = validate_config(self.config)
        if errors:
            raise ConfigError("; ".join(errors))
        self.hash = config_hash(self.config)
        self.version = 1
        self.last_error = ""

    def _read(self) -> Dict[str, Any]:
        return load_config(*self._paths)

    def reload_if_changed(self) -> bool:
        try:
            candidate = self._read()
        except (ConfigError, OSError, ValueError) as exc:
            # 文件被替换/写了一半/暂时不可读：保留旧值，下一轮再试
            self.last_error = "{}: {}".format(type(exc).__name__, exc)
            return False
        candidate_hash = config_hash(candidate)
        if candidate_hash == self.hash:
            return False
        errors = validate_config(candidate)
        if errors:
            self.last_error = "; ".join(errors)  # 保留旧配置，运行不受影响
            return False
        self.config = candidate
        self.hash = candidate_hash
        self.version += 1
        return True

    def snapshot(self, conn, source: str = "startup") -> int:
        cur = conn.execute(
            "INSERT INTO config_snapshots(config_hash, source, payload, created_at) VALUES (?,?,?,?)",
            (self.hash, source, json.dumps(self.config, ensure_ascii=False, sort_keys=True), time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)
