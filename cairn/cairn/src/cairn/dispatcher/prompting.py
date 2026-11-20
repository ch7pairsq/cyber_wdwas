from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_prompt(group: str, name: str) -> str:
    """从包内 prompts 资源目录读取提示词模板。"""
    return resources.files("cairn.dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    """按 {key} 占位符替换渲染模板。"""
    text = template
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return text


def format_fact_ids(fact_ids: list[str]) -> str:
    """已确认事实 id 列表 → JSON 块文本。"""
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    """开 Intent 列表 → JSON 块文本。"""
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    """提示列表 → JSON 块文本。"""
    return format_json_block(hints)


def format_json_block(value: Any) -> str:
    """任意值 → 缩进 JSON 文本（提示词注入用）。"""
    return json.dumps(value, ensure_ascii=False, indent=2)
