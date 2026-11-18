# 固定此次实际校验的 Linux amd64 基础镜像摘要；部署前仍需确认平台架构。
ARG BASE_IMAGE=ghcr.io/astral-sh/uv@sha256:b1d470137757f15474bbcfd9596f49885f1d874f6a9e17d505c1e7e99fe2fc5b
FROM ${BASE_IMAGE} AS runtime

# 工具链层级：core=最小可用，ctf=核心 CTF 工具，full=预留可选层。
ARG TOOLCHAIN_TIER=ctf

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app:/app/cairn/cairn/src" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# 仅构建阶段联网安装，启动命令中不得安装依赖。
COPY docker/toolchain.json docker/install_toolchain.py /app/docker/
RUN python /app/docker/install_toolchain.py --tier "${TOOLCHAIN_TIER}" \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /bin/bash agent

WORKDIR /app
COPY docker/requirements-runtime.lock /app/docker/requirements-runtime.lock
COPY cairn/cairn /app/cairn/cairn
RUN pip install --no-cache-dir -r docker/requirements-runtime.lock \
    && pip install --no-cache-dir --no-deps ./cairn/cairn \
    && pip check \
    && pip freeze > /opt/installed-freeze.txt

COPY ctf_ext /app/ctf_ext
COPY config /app/config
COPY tests /app/tests
COPY scripts /app/scripts
COPY cairn/VENDORED.md cairn/LICENSE /app/cairn/
COPY requirements-lock.txt pyproject.toml README.md /app/
COPY docker /app/docker
RUN mkdir -p /data /var/lib/cairn /workspaces /app/build \
    && chown 10001:10001 /data /var/lib/cairn /workspaces /app/build

FROM runtime AS development
RUN pip install --no-cache-dir -r docker/requirements.lock \
    && pip check && pip freeze > /opt/installed-freeze.txt
# 上游测试仅解析此样例，不据此启动真实 CLI；不加入 competition 分支。
COPY cairn/dispatch.local.example.yaml /app/cairn/dispatch.local.example.yaml
USER 10001:10001
CMD ["python", "-m", "ctf_ext", "run", "--mode", "mock", "--seconds", "30", "--db", "/data/practice.db", "--evidence", "/data/evidence", "--report", "/data/report.json", "--dashboard", "/data/dashboard.html"]

FROM runtime AS competition
# 安装器只用于构建；候选镜像运行时移除 pip/uv/ensurepip/apt 等入口。
RUN python /app/docker/strip_installers.py \
    && python /app/docker/check_competition.py
ENV PYTHONNOUSERSITE=1 PIP_NO_INDEX=1 UV_OFFLINE=1
LABEL io.cyber-wdwas.release-status="blocked-awaiting-official-harness"
USER 10001:10001
ENTRYPOINT ["python", "-m", "ctf_ext.competition"]
