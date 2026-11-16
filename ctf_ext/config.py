"""配置加载与校验（§13：分层配置 + 校验；X 缺失即 Fail Closed）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List


class ConfigError(Exception):
    pass


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required to read {}: pip install pyyaml".format(path)
        ) from exc
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_json(path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """返回错误列表；空列表表示可运行。检查项对应附录 F-6。"""
    errors: List[str] = []
    conc = cfg.get("concurrency") or {}
    if int(conc.get("max_workers", 0)) < 1:
        errors.append("concurrency.max_workers must be >= 1")
    if int(conc.get("max_project_workers", 0)) < 1:
        errors.append("concurrency.max_project_workers must be >= 1")
    dl = cfg.get("download") or {}
    for key in ("max_single_bytes", "max_total_bytes"):
        if int(dl.get(key, 0)) < 0:
            errors.append("download.{} must be >= 0 (0 = fail closed)".format(key))
    harness = cfg.get("harness") or {}
    if harness.get("protocol") not in ("mock", "official"):
        errors.append("harness.protocol must be 'mock' or 'official'")
    if harness.get("protocol") == "official" and not harness.get("official_sdk_ready"):
        errors.append("harness.protocol=official requires official_sdk_ready=true (SDK not yet obtained)")
    if "competition_locked" not in (cfg.get("harness") or {}):
        errors.append("harness.competition_locked must be set explicitly (true for competition)")
    return errors
