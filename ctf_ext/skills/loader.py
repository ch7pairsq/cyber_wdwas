"""Skill 索引先行、按需加载（§10：只取所需章节，不全量注入）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

_SKILLS_DIR = Path(__file__).resolve().parent


class SkillNotFound(Exception):
    pass


class SkillLoader:
    def __init__(self, skills_dir: Optional[Path] = None):
        self._dir = Path(skills_dir) if skills_dir else _SKILLS_DIR
        with (self._dir / "index.json").open("r", encoding="utf-8") as fh:
            self._index: List[Dict[str, object]] = json.load(fh)

    def list_index(self) -> List[Dict[str, object]]:
        """索引给规划器看：名称/领域/前置/预估大小/风险，不含正文。"""
        return [dict(name=s["name"], domain=s["domain"], prerequisites=s["prerequisites"],
                     tokens_hint=s["tokens_hint"], risk=s["risk"]) for s in self._index]

    def find(self, name: str) -> Dict[str, object]:
        for s in self._index:
            if s["name"] == name:
                return s
        raise SkillNotFound(name)

    def load_section(self, name: str, max_bytes: int = 20000) -> str:
        """按需加载单节正文；超限报错而非静默截断。"""
        meta = self.find(name)
        path = self._dir / "sections" / (str(meta["file"]))
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise ValueError("skill {} is {} bytes > cap {}".format(name, len(data), max_bytes))
        return data.decode("utf-8")
