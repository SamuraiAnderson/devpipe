"""静态 HTML 报告 (CORE-11)。

从一份 NDJSON 生成**自包含**的 HTML：
- 数据以 ``<script type="application/json" id="run-data">`` 内嵌；
- CSS / JS 均内联；无 http/https 外链；无网络请求。

不依赖 FastAPI / Jinja2；仅使用标准库 + ``timeline.js`` 静态资源。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

_STATIC_DIR = Path(__file__).parent / "static"


def render_report(ndjson_path: Path, output_path: Path) -> None:
    """把 ``ndjson_path`` 里的记录序列化到 ``output_path`` 的自包含 HTML。"""
    records = _load_ndjson(ndjson_path)
    meta = _extract_meta(records)
    css = _read_static("styles.css")
    js = _read_static("timeline.js")
    # 分栏拖动依赖 Split.js；内联进来报告才能在 file:// 下照样拖
    split_js = _read_static("vendor/split.min.js")
    data_json = json.dumps({"meta": meta, "records": records}, ensure_ascii=False)
    raw_body = "\n".join(_format_line(r) for r in records)
    title = html.escape(meta.get("name") or ndjson_path.stem)
    html_doc = _HTML_TEMPLATE.format(
        title=title,
        css=css,
        split_js=split_js,
        js=js,
        data_json=data_json,
        raw_body=html.escape(raw_body),
    )
    output_path.write_text(html_doc, encoding="utf-8")


def _load_ndjson(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover - 半行降级
                continue
    return records


def _extract_meta(records: list[dict]) -> dict:
    meta: dict = {"name": None, "started_at": None, "ended_at": None, "exception": None}
    for r in records:
        if r.get("event") == "script.begin":
            meta["name"] = r.get("name")
            meta["started_at"] = r.get("started_at") or r.get("timestamp")
            meta["pid"] = r.get("pid")
        elif r.get("event") == "script.end":
            meta["ended_at"] = r.get("ended_at") or r.get("timestamp")
            meta["exception"] = r.get("exception")
    return meta


def _read_static(name: str) -> str:
    path = _STATIC_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _format_line(rec: dict) -> str:
    ts = rec.get("timestamp")
    event = rec.get("event", "?")
    session = rec.get("session_id", "-")
    msg = rec.get("message", "")
    return f"{ts} [{event}] [{session}] {msg}"


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>{title} — RedPyMake report</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <p class="hint">RedPyMake 静态运行报告 — 自包含 HTML，无外链。</p>
</header>
<main>
  <section id="timeline-root"></section>
  <section id="raw-log">
    <h2>原始 NDJSON</h2>
    <pre>{raw_body}</pre>
  </section>
</main>
<script type="application/json" id="run-data">{data_json}</script>
<script>{split_js}</script>
<script>{js}</script>
</body>
</html>
"""


__all__ = ["render_report"]
