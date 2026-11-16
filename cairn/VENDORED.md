# Vendored: Cairn

- 上游：https://github.com/oritera/Cairn
- Commit：`8e7e0ea67552383851dfcabfba0c4e9c8d007878`（2026-11-17 拉取）
- 源码包 sha256：`08f6cf8e573a753f97f6b5f74d4bdf930bfb92427808af3fce800e04211a8e5a`
- 许可证：AGPL-3.0（见 ./LICENSE）；分发与二次修改须履行 AGPL 义务，合规确认归项目负责人
- 重新拉取：`bash scripts/vendor_cairn.sh`（固定 commit，校验源码包哈希）

## 本目录保持上游原样

不修改、不内联补丁；本地扩展一律在 `ctf_ext/`，避免升级时冲突。

## 运行前提（F-2 现状）

上游 `cairn/pyproject.toml` 声明 `requires-python = ">=3.12"`，依赖 fastapi / uvicorn / docker 等，
入口为 `cairn = "cairn.cli:main"`，运行方式见上游 README：

```bash
uv run --project cairn cairn dispatch --config dispatch.yaml
```

当前开发机为 Python 3.9.6 且无 Docker，**无法在本机执行 serve/dispatch 冒烟**；
生产镜像须用 3.12+（或不低于上游要求的 3.13 容器）。该项不是代码缺陷，是环境阻塞，
已在 README「与设计文档的差异」与 preflight 中标注为 WARN。
