# 技能草稿目录

- 这里只放人工审核前的外部方法草稿，不进入运行时。
- 生成：`python3 scripts/import_skill_draft.py --input <local.md> --repo <url> --commit <sha> --license <name>`
- 草稿不会自动加入 `ctf_ext/skills/index.json`；必须人工改写、过 `check_skills.py --strict` 后才能移入 `sections/`。
- 不提交外部代码块、shell 命令、字面 flag 或大段原文。
