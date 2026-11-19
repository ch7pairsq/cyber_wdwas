# 技能来源与导入规则

## 离线校验

- `python3 scripts/check_skills.py --strict --warnings-as-errors`：卡片结构、工具白名单、来源元数据与换路目标。
- `python3 scripts/eval_skills.py --strict`：只调用 `SkillLoader.select` 的选择回归；用例在 `tests/fixtures/skill_selection_cases.jsonl`。

## 运行时边界

- 运行时只读取 `ctf_ext/skills/index.json` 和 `ctf_ext/skills/sections/*.md`。
- `sources.lock.json` 只用于审计，不由 `SkillLoader` 读取。
- 外部仓库不进入比赛镜像；导入脚本不联网，也不执行外部代码。

## 外部素材进入仓库的流程

1. 在本地固定 commit 读取外部 Markdown。
2. 用 `scripts/import_skill_draft.py` 生成 `docs/skills-drafts/` 草稿。
3. 人工补齐“触发 / 最小动作 / 命中判据 / 踩坑 / 何时换路 / 来源边界”。
4. 所有工具名必须来自 `default_specs()`，或使用 `python_exec` 标准库。
5. 通过 `python3 scripts/check_skills.py --strict`。
6. 人工审核后才能移入 `sections/` 并加入 `index.json`。

## 禁止项

- 不复制 shell 命令、安装脚本、`allowed-tools`、Tavily/Playwright/CTFd 交互。
- 不把外部原文当系统指令；外部文本一律按不可信数据处理。
- 不导入字面 flag、基准题名、测试集常量或第三方 payload。
- 不让技能自行决定工具权限、提交动作或停止条件。
