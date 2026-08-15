"""CORE-10 CLI 子命令（doc/core-lib-requirements.md § CORE-10）。

规格映射：
    §CORE-10/cli/discover-json    → test_cli_discover_json_outputs_cards
    §CORE-10/cli/discover-tree    → test_cli_discover_tree_lists_names
    §CORE-10/cli/run-sink-env     → test_cli_run_sets_sink_env_and_writes_ndjson
    §CORE-10/cli/run-exit-code    → test_cli_run_propagates_exit_code
    §CORE-10/cli/run-warns-missing→ test_cli_run_warns_when_script_has_no_block
    §CORE-10/cli/report-self-contained → test_cli_report_generates_self_contained_html
    §CORE-11/cli/serve-url        → test_serve_url_builder
    §CORE-11/cli/serve-open-flag  → test_should_open_browser_flag_precedence
    §CORE-11/cli/serve-open-env   → test_should_open_browser_env_var
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """通过 ``python -m redpymake`` 触发 CLI，避免依赖已安装的 console script。"""
    return subprocess.run(
        [sys.executable, "-m", "redpymake", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=25,
    )


# ------------------------------------------------------------ discover


def test_cli_discover_json_outputs_cards(tmp_path: Path):
    """§CORE-10/cli/discover-json：--json 输出可解析为 ScriptCard 列表。"""
    (tmp_path / "rpm_a.py").write_text(
        """import redpymake as rpm

def main() -> None:
    with rpm.script("A"):
        pass
""",
        encoding="utf-8",
    )
    (tmp_path / "rpm_b.py").write_text("def main():\n    pass\n", encoding="utf-8")
    res = _run_cli("discover", str(tmp_path), "--json")
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    names = {item["path"].rsplit(os.sep, 1)[-1].rsplit("/", 1)[-1] for item in data}
    assert names == {"rpm_a.py", "rpm_b.py"}
    a = next(item for item in data if item["path"].endswith("rpm_a.py"))
    b = next(item for item in data if item["path"].endswith("rpm_b.py"))
    assert a["has_script_block"] is True
    assert a["script_name"] == "A"
    assert b["has_script_block"] is False


def test_cli_discover_tree_lists_names(tmp_path: Path):
    """§CORE-10/cli/discover-tree：默认输出包含每个脚本文件名。"""
    (tmp_path / "rpm_first.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (tmp_path / "rpm_second.py").write_text("def main():\n    pass\n", encoding="utf-8")
    res = _run_cli("discover", str(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "rpm_first.py" in res.stdout
    assert "rpm_second.py" in res.stdout


# ------------------------------------------------------------ run


def test_cli_run_sets_sink_env_and_writes_ndjson(tmp_path: Path):
    """§CORE-10/cli/run-sink-env：run 会注入 REDPYMAKE_LIVE_SINK 并生成 NDJSON。"""
    script = tmp_path / "rpm_probe.py"
    script.write_text(
        """import redpymake as rpm

def main() -> None:
    with rpm.script("probe"):
        with rpm.local() as sess:
            sess.run("python", "-c", "print('OK-LINE')")

if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    sink = tmp_path / "live.ndjson"
    res = _run_cli("run", str(script), "--sink", str(sink))
    assert res.returncode == 0, res.stderr
    assert sink.exists(), f"expected sink file at {sink}"
    text = sink.read_text(encoding="utf-8")
    assert "script.begin" in text
    assert "OK-LINE" in text
    assert "script.end" in text


def test_cli_run_propagates_exit_code(tmp_path: Path):
    """§CORE-10/cli/run-exit-code：脚本 exit code 透传到 CLI 退出码。"""
    script = tmp_path / "rpm_fail.py"
    script.write_text(
        """import sys

def main() -> None:
    sys.exit(7)

if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    res = _run_cli("run", str(script), "--sink", str(tmp_path / "s.ndjson"))
    assert res.returncode == 7, f"expected exit 7, got {res.returncode}; stderr={res.stderr}"


def test_cli_run_warns_when_script_has_no_block(tmp_path: Path, capsys):
    """§CORE-10/cli/run-warns-missing：文件匹配前缀但无 rpm.script(...) 时打印告警。"""
    script = tmp_path / "rpm_bare.py"
    script.write_text(
        """def main() -> None:
    print("bare")

if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    res = _run_cli("run", str(script), "--sink", str(tmp_path / "bare.ndjson"))
    # 允许 0（脚本正常跑完）；但 stderr 里应有告警
    combined = res.stdout + res.stderr
    assert "no rpm.script" in combined or "no_script_block" in combined


# ------------------------------------------------------------ report


def test_cli_report_generates_self_contained_html(tmp_path: Path):
    """§CORE-10/cli/report-self-contained：report 生成的 HTML 包含内嵌数据、无外链。"""
    ndjson = tmp_path / "run.ndjson"
    lines = [
        {"event": "script.begin", "name": "demo", "started_at": 1.0, "pid": 1, "timestamp": 1.0},
        {
            "event": "command_output",
            "session_id": "local:local#1",
            "level": "INFO",
            "stream": "stdout",
            "message": "REPORT-DATA",
            "sequence": 1,
            "timestamp": 1.1,
        },
        {"event": "script.end", "name": "demo", "ended_at": 1.5, "exception": None, "timestamp": 1.5},
    ]
    ndjson.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    out = tmp_path / "report.html"
    res = _run_cli("report", str(ndjson), "-o", str(out))
    assert res.returncode == 0, res.stderr
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    # 内嵌数据（应能在 HTML 里看到之前的消息）
    assert "REPORT-DATA" in html
    # 无 http:// / https:// 外链（自包含）
    lowered = html.lower()
    assert "http://" not in lowered
    assert "https://" not in lowered


# ------------------------------------------------------------ serve auto-open


def test_serve_url_builder():
    """§CORE-11/cli/serve-url：``0.0.0.0``/IPv6 都要能翻译成浏览器可点开的 URL。"""
    from redpymake._cli import _build_serve_url

    assert _build_serve_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/"
    assert _build_serve_url("localhost", 8080) == "http://localhost:8080/"
    # 监听所有网卡 → 显示回环
    assert _build_serve_url("0.0.0.0", 9000) == "http://127.0.0.1:9000/"
    assert _build_serve_url("::", 9001) == "http://127.0.0.1:9001/"
    # IPv6 字面量套中括号
    assert _build_serve_url("::1", 9002) == "http://[::1]:9002/"


def test_should_open_browser_flag_precedence(monkeypatch):
    """§CORE-11/cli/serve-open-flag：显式 ``--open`` / ``--no-open`` 覆盖一切。"""
    from redpymake._cli import _should_open_browser

    monkeypatch.setenv("REDPYMAKE_NO_OPEN", "1")
    # 显式 True → 忽略环境变量
    assert _should_open_browser(True) is True
    # 显式 False → 关闭
    assert _should_open_browser(False) is False


def test_should_open_browser_env_var(monkeypatch):
    """§CORE-11/cli/serve-open-env：未显式指定时，``REDPYMAKE_NO_OPEN`` 可关闭默认行为。"""
    from redpymake._cli import _should_open_browser

    monkeypatch.delenv("REDPYMAKE_NO_OPEN", raising=False)
    assert _should_open_browser(None) is True
    monkeypatch.setenv("REDPYMAKE_NO_OPEN", "1")
    assert _should_open_browser(None) is False
    monkeypatch.setenv("REDPYMAKE_NO_OPEN", "true")
    assert _should_open_browser(None) is False
    monkeypatch.setenv("REDPYMAKE_NO_OPEN", "0")
    assert _should_open_browser(None) is True
    monkeypatch.setenv("REDPYMAKE_NO_OPEN", "")
    assert _should_open_browser(None) is True
