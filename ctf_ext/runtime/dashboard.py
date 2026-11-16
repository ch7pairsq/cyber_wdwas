"""§18 运行看板：从 SQLite 快照生成自包含 HTML（无外部资源、无追踪）。

看板是静态快照，不是实时监控；数值来自黑板/事件表，压测数据由 bench 脚本提供。
视觉：极光暗色（近黑底 + 低饱和发光渐变 + 单色强调），不引入图表库。
"""
from __future__ import annotations

import html
import json
import os
import time
from typing import Any, Dict, List, Optional

CSS = """
:root {
  --bg: #06080c; --surface: #0c1118; --surface-2: #0f1520; --line: #1c2634;
  --text: #e8eef6; --muted: #8b98a9; --dim: #5d6a7c;
  --cyan: #7dd3fc; --violet: #a78bfa; --green: #6ee7b7; --amber: #fbbf24; --red: #fb7185;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.55 -apple-system, "PingFang SC", "Noto Sans SC", sans-serif;
  min-height: 100vh;
}
body::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background:
    radial-gradient(680px 380px at 12% -6%, rgba(125,211,252,.16), transparent 65%),
    radial-gradient(720px 420px at 92% 4%, rgba(167,139,250,.14), transparent 62%),
    radial-gradient(900px 520px at 50% 108%, rgba(110,231,183,.07), transparent 70%);
}
.wrap { position: relative; z-index: 1; max-width: 1180px; margin: 0 auto; padding: 48px 28px 56px; }
header { display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-end; justify-content: space-between; }
.eyebrow { font: 600 11px/1 var(--mono); letter-spacing: .22em; color: var(--cyan); text-transform: uppercase; }
h1 { margin: 10px 0 6px; font-size: 30px; letter-spacing: -.01em; font-weight: 650; }
.sub { color: var(--muted); max-width: 620px; margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  font: 500 11px/1 var(--mono); color: var(--muted); padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 999px; background: rgba(12,17,24,.7);
}
.chip b { color: var(--text); font-weight: 600; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin: 30px 0 0; }
.card {
  background: linear-gradient(180deg, rgba(15,21,32,.92), rgba(12,17,24,.92));
  border: 1px solid var(--line); border-radius: 14px; padding: 18px 18px 16px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.03);
}
.kpi-label { color: var(--muted); font-size: 12px; letter-spacing: .04em; }
.kpi-value { font: 600 30px/1.1 var(--mono); margin: 8px 0 6px; letter-spacing: -.02em; }
.kpi-note { color: var(--dim); font-size: 12px; }
.section { margin-top: 26px; background: rgba(12,17,24,.86); border: 1px solid var(--line); border-radius: 14px; padding: 20px; }
.section h2 { margin: 0 0 4px; font-size: 15px; font-weight: 600; letter-spacing: .01em; }
.section p.note { margin: 0 0 16px; color: var(--dim); font-size: 12px; }
.bar { display: flex; height: 12px; border-radius: 999px; overflow: hidden; background: #0a0f16; border: 1px solid var(--line); }
.bar span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; color: var(--muted); font: 12px/1 var(--mono); }
.legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 7px; }
.grid2 { display: grid; grid-template-columns: 1.25fr .75fr; gap: 20px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th { text-align: left; color: var(--dim); font: 600 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; padding: 0 10px 10px 0; }
td { padding: 9px 10px 9px 0; border-top: 1px solid rgba(28,38,52,.7); color: var(--text); vertical-align: top; }
td.mono, .mono { font-family: var(--mono); }
td.muted { color: var(--muted); }
.tag { font: 600 10px/1 var(--mono); padding: 5px 7px; border-radius: 6px; letter-spacing: .06em; }
.tag.ACC { color: #052e21; background: var(--green); }
.tag.REJ { color: #3b0a12; background: var(--red); }
.tag.UNK { color: #3a2a04; background: var(--amber); }
.empty { color: var(--dim); font-size: 12.5px; padding: 14px 0; }
footer { margin-top: 30px; color: var(--dim); font-size: 11.5px; display: flex; flex-wrap: wrap; gap: 10px; justify-content: space-between; }
@media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } .wrap { padding: 32px 18px 40px; } h1 { font-size: 25px; } }
@media (prefers-reduced-motion: no-preference) { .card { transition: border-color .2s ease; } .card:hover { border-color: #2a3a4f; } }
"""

STATUS_COLORS = {
    "COMPLETED": "var(--green)", "RUNNING": "var(--cyan)", "READY": "var(--violet)",
    "FAILED_RETRYABLE": "var(--amber)", "FAILED": "var(--red)",
}


def collect_snapshot(conn) -> Dict[str, Any]:
    rows = lambda sql, args=(): [dict(r) for r in conn.execute(sql, args).fetchall()]
    intents = {r["status"]: r["c"] for r in rows(
        "SELECT status, COUNT(*) c FROM intents_ext GROUP BY status")}
    outcomes = {r["outcome"]: r["c"] for r in rows(
        "SELECT outcome, COUNT(*) c FROM attempts GROUP BY outcome")}
    submissions = {r["status"]: r["c"] for r in rows(
        "SELECT status, COUNT(*) c FROM submissions GROUP BY status")}
    events = rows("SELECT event_type, entity_id, created_at FROM events ORDER BY id DESC LIMIT 12")
    config = rows("SELECT config_hash, source, created_at FROM config_snapshots ORDER BY version DESC LIMIT 1")
    tokens = 0
    tools = 0
    for row in conn.execute("SELECT resource_usage FROM attempts"):
        try:
            usage = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        tokens += int(usage.get("model_tokens") or 0)
        tools += int(usage.get("tool_calls") or 0)
    facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    return {
        "generated_at": time.time(), "intents": intents, "outcomes": outcomes,
        "submissions": submissions, "events": events, "config": config[0] if config else None,
        "facts": facts, "model_tokens": tokens, "tool_calls": tools,
        "attempts": sum(outcomes.values()),
    }


def _bar(intents: Dict[str, int]) -> str:
    total = sum(intents.values()) or 1
    order = ["COMPLETED", "RUNNING", "READY", "FAILED_RETRYABLE", "FAILED"]
    parts, legend = [], []
    for status in order:
        count = intents.get(status, 0)
        if not count:
            continue
        width = max(2.0, count * 100.0 / total)
        parts.append('<span style="width:{:.1f}%;background:{};" title="{}: {}"></span>'.format(
            width, STATUS_COLORS[status], status, count))
        legend.append('<span><i style="background:{}"></i>{} <b class="mono">{}</b></span>'.format(
            STATUS_COLORS[status], status, count))
    if not parts:
        return '<div class="empty">尚未产生 Intent。</div>'
    return '<div class="bar">' + "".join(parts) + '</div><div class="legend">' + "".join(legend) + "</div>"


def _event_rows(events: List[Dict[str, Any]]) -> str:
    if not events:
        return '<tr><td colspan="3" class="empty">暂无事件。</td></tr>'
    out = []
    for ev in events:
        ts = time.strftime("%H:%M:%S", time.localtime(ev["created_at"]))
        out.append('<tr><td class="mono muted">{}</td><td class="mono">{}</td>'
                   '<td class="muted">{}</td></tr>'.format(
                       ts, html.escape(str(ev["event_type"])), html.escape(str(ev["entity_id"] or "—"))))
    return "".join(out)


def _submission_rows(submissions: Dict[str, int]) -> str:
    if not submissions:
        return '<tr><td colspan="2" class="empty">尚无提交。</td></tr>'
    out = []
    for status in ("ACCEPTED", "REJECTED", "UNKNOWN", "SENDING"):
        count = submissions.get(status, 0)
        if not count:
            continue
        cls = {"ACCEPTED": "ACC", "REJECTED": "REJ"}.get(status, "UNK")
        out.append('<tr><td><span class="tag {}">{}</span></td>'
                   '<td class="mono">{}</td></tr>'.format(cls, status, count))
    return "".join(out)


def _bench_table(bench: Dict[str, Any]) -> str:
    rows = bench.get("results") or []
    if not rows:
        return '<div class="empty">尚无压测数据：运行 python3 scripts/bench.py。</div>'
    body = []
    for r in rows:
        body.append(
            '<tr><td class="mono">{w}</td><td class="mono">{n}</td><td class="mono">{s:.2f}s</td>'
            '<td class="mono">{c:.1f}</td><td class="mono">{u:.0%}</td>'
            '<td class="mono" style="color:{color}">{e}</td></tr>'.format(
                w=r["workers"], n=r["intents"], s=r["seconds"], c=r["claims_per_sec"],
                u=r["utilization"], e=r["errors"], color="var(--red)" if r["errors"] else "var(--green)"))
    return ('<table><thead><tr><th>Workers</th><th>Intents</th><th>耗时</th>'
            '<th>领取/秒</th><th>占用率</th><th>错误</th></tr></thead><tbody>'
            + "".join(body) + "</tbody></table>")


def render_dashboard(snapshot: Dict[str, Any], bench: Optional[Dict[str, Any]] = None,
                     db_path: str = "") -> str:
    intents = snapshot["intents"]
    total = sum(intents.values())
    completed = intents.get("COMPLETED", 0)
    accepted = snapshot["submissions"].get("ACCEPTED", 0)
    config = snapshot["config"] or {}
    cfg_hash = (config.get("config_hash") or "unpinned")[:12]
    generated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot["generated_at"]))
    tokens = "{:,}".format(snapshot["model_tokens"])
    tool_calls = "{:,}".format(snapshot["tool_calls"])
    kpi = [
        ("Intent 完成", "{}/{}".format(completed, total), "尝试 {} 次 · 事实 {} 条".format(snapshot["attempts"], snapshot["facts"])),
        ("提交受理", str(accepted), "共 {} 次提交记录".format(sum(snapshot["submissions"].values()))),
        ("模型 Token", tokens, "工具调用 {} 次".format(tool_calls)),
        ("配置版本", cfg_hash, "来源：{}".format(html.escape(str(config.get("source") or "—")))),
    ]
    kpi_html = "".join(
        '<div class="card"><div class="kpi-label">{}</div><div class="kpi-value">{}</div>'
        '<div class="kpi-note">{}</div></div>'.format(l, v, n) for l, v, n in kpi)
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ctf-agent · 运行看板</title><style>{css}</style></head>
<body><div class="wrap">
<header>
  <div>
    <div class="eyebrow">ctf-agent · runtime board</div>
    <h1>运行看板</h1>
    <p class="sub">来自本地黑板的只读快照：Intent 生命周期、证据与提交状态、配置版本，以及单机并发压测结果。这是静态导出，不是实时探针。</p>
  </div>
  <div class="chips">
    <span class="chip">SNAPSHOT <b>{generated}</b></span>
    <span class="chip">DB <b>{db}</b></span>
    <span class="chip">CFG <b>{cfg_hash}</b></span>
  </div>
</header>
<div class="kpis">{kpi}</div>
<div class="section">
  <h2>Intent 状态分布</h2>
  <p class="note">FAILED_RETRYABLE 会由看门狗重新入队；RUNNING 持有租约，崩溃后按 lease 回收。</p>
  {bar}
</div>
<div class="grid2">
  <div class="section">
    <h2>最近事件</h2>
    <p class="note">events 表只追加，用于审计与复盘。</p>
    <table><thead><tr><th>时间</th><th>类型</th><th>实体</th></tr></thead>
    <tbody>{events}</tbody></table>
  </div>
  <div class="section">
    <h2>提交状态</h2>
    <p class="note">UNKNOWN 只查询回执，绝不重发。</p>
    <table><tbody>{subs}</tbody></table>
    <h2 style="margin-top:22px">尝试结果</h2>
    <p class="note">no_signal 会释放方向，success 才落为完成。</p>
    <div class="mono" style="font-size:12.5px;color:var(--muted)">{outcomes}</div>
  </div>
</div>
<div class="section">
  <h2>并发压测（4 / 6 / 8）</h2>
  <p class="note">单机 SQLite 模拟并发：验证领取互斥、写结果不丢、无 database-locked 错误。不代表平台吞吐或真实解题速度。</p>
  {bench}
</div>
<footer><span>生成于 {generated} · 数据文件 {db}</span><span>只读快照 · 无外部资源 · 无追踪</span></footer>
</div></body></html>""".format(
        css=CSS, generated=generated, db=html.escape(db_path or "—"), cfg_hash=cfg_hash,
        kpi=kpi_html, bar=_bar(intents), events=_event_rows(snapshot["events"]),
        subs=_submission_rows(snapshot["submissions"]),
        outcomes=html.escape(", ".join("{}={}".format(k, v) for k, v in sorted(snapshot["outcomes"].items())) or "—"),
        bench=_bench_table(bench or {}),
    )


def write_dashboard(conn, path: str, bench_path: Optional[str] = None, db_path: str = "") -> str:
    bench = None
    if bench_path and os.path.exists(bench_path):
        with open(bench_path, "r", encoding="utf-8") as fh:
            bench = json.load(fh)
    html_text = render_dashboard(collect_snapshot(conn), bench=bench, db_path=db_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return path
