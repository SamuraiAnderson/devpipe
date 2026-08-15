"""顶层 CLI (CORE-10 / CORE-11)。

四个子命令：

- ``redpymake discover [ROOT] [--json]``：AST 发现，输出树或 JSON；
- ``redpymake run PATH [--sink FILE]``：子进程运行单个脚本，环境变量注入
  ``REDPYMAKE_LIVE_SINK`` 使 CORE-10 sink 生效；透传脚本 exit code；
- ``redpymake report NDJSON -o OUT.html``：从 NDJSON 生成自包含 HTML 报告；
- ``redpymake serve [ROOT] [--host --port]``：起 FastAPI Web UI（依赖 ``[web]`` extra）；
  每次启动另起一份活跃日志（上一份为空则复用），``--resume-log`` 可续用上次那份。

同时提供 ``python -m redpymake`` 入口（见 ``__main__.py``）。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Sequence

from ._discover import ScriptCard, discover


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redpymake",
        description="RedPyMake 脚本发现 / 运行 / 可视化 CLI",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # discover
    p_disc = subs.add_parser("discover", help="扫描目录，列出所有 rpm_*.py 脚本")
    p_disc.add_argument("root", nargs="?", default=".", help="扫描根目录（默认当前）")
    p_disc.add_argument("--json", action="store_true", help="输出 JSON 列表")
    p_disc.set_defaults(func=_cmd_discover)

    # run
    p_run = subs.add_parser("run", help="子进程运行单个脚本 + 落 NDJSON")
    p_run.add_argument("path", help="脚本文件路径（.py）")
    p_run.add_argument(
        "--sink",
        default=None,
        help="NDJSON 输出路径；缺省用 <script_dir>/.redpymake/runs/<name>-<ts>.ndjson",
    )
    p_run.set_defaults(func=_cmd_run)

    # report
    p_rep = subs.add_parser("report", help="从 NDJSON 生成自包含 HTML 报告")
    p_rep.add_argument("ndjson", help="输入 NDJSON 路径")
    p_rep.add_argument("-o", "--output", required=True, help="输出 HTML 路径")
    p_rep.set_defaults(func=_cmd_report)

    # serve
    p_srv = subs.add_parser("serve", help="启动 Web UI（需要 redpymake[web]）")
    p_srv.add_argument("root", nargs="?", default=".", help="workspace 根目录")
    p_srv.add_argument("--host", default="127.0.0.1", help="监听主机（默认 127.0.0.1）")
    p_srv.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    # 浏览器自动打开：默认开启；--no-open 关闭（CI / SSH 远程 / headless 场景）。
    # 环境变量 REDPYMAKE_NO_OPEN=1 亦可关闭；BROWSER 环境变量由 webbrowser 模块原生消费。
    p_srv.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        default=None,
        help="启动后自动用系统默认浏览器打开首页（默认开启）",
    )
    p_srv.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="启动后不打开浏览器（CI / SSH 远程 / headless 场景）",
    )
    p_srv.add_argument(
        "--resume-log",
        action="store_true",
        help="续用上次的活跃日志（默认每次启动另起一份，旧的留作历史）",
    )
    p_srv.set_defaults(func=_cmd_serve)

    # logs 子命令族（§CORE-11 日志组）
    p_logs = subs.add_parser("logs", help="workspace 日志组管理（§CORE-11）")
    logs_subs = p_logs.add_subparsers(dest="logs_command", required=True)

    p_logs_list = logs_subs.add_parser("list", help="列出全部日志组")
    p_logs_list.add_argument("root", nargs="?", default=".", help="workspace 根目录")
    p_logs_list.add_argument("--json", action="store_true", help="JSON 输出")
    p_logs_list.set_defaults(func=_cmd_logs_list)

    p_logs_rn = logs_subs.add_parser("rename", help="重命名日志组")
    p_logs_rn.add_argument("root", nargs="?", default=".", help="workspace 根目录")
    p_logs_rn.add_argument("log_id", help="目标日志组 id")
    p_logs_rn.add_argument("name", help="新显示名（不能为空）")
    p_logs_rn.add_argument("--description", default=None, help="可选描述")
    p_logs_rn.set_defaults(func=_cmd_logs_rename)

    p_logs_rot = logs_subs.add_parser("rotate", help="归档当前活跃日志并新建（清空）")
    p_logs_rot.add_argument("root", nargs="?", default=".", help="workspace 根目录")
    p_logs_rot.add_argument("--name", default=None, help="新日志显示名（默认时间戳）")
    p_logs_rot.set_defaults(func=_cmd_logs_rotate)

    p_logs_pin = logs_subs.add_parser("pin", help="pin / unpin 日志组（拒绝被 discard）")
    p_logs_pin.add_argument("root", nargs="?", default=".", help="workspace 根目录")
    p_logs_pin.add_argument("log_id", help="目标日志组 id")
    p_logs_pin.add_argument("--no-pin", dest="pin", action="store_false", default=True)
    p_logs_pin.set_defaults(func=_cmd_logs_pin)

    p_logs_del = logs_subs.add_parser("discard", help="硬删日志组（活跃或 pinned 时拒绝）")
    p_logs_del.add_argument("root", nargs="?", default=".", help="workspace 根目录")
    p_logs_del.add_argument("log_id", help="目标日志组 id")
    p_logs_del.set_defaults(func=_cmd_logs_discard)

    args = parser.parse_args(argv)
    return args.func(args)


# ------------------------------------------------------------ discover


def _cmd_discover(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    cards = discover(root)
    if args.json:
        print(json.dumps([c.to_dict() for c in cards], ensure_ascii=False, indent=2))
        return 0
    if not cards:
        print(f"(no rpm_*.py under {root})")
        return 0
    for card in cards:
        rel = card.path.relative_to(root) if _try_relative(card.path, root) else card.path
        prefix = "[ok]" if card.has_script_block else "[warn]"
        name = card.script_name or "(unnamed)"
        factories = ",".join(card.factories) if card.factories else "-"
        line = f"{prefix} {rel}    name={name}  factories=[{factories}]"
        try:
            print(line)
        except UnicodeEncodeError:  # pragma: no cover - Windows GBK 等极端本地化终端
            sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
        if card.error:
            print(f"    error: {card.error}")
        if not card.has_script_block and not card.error:
            print(f"    (no rpm.script(...) block; will not be run by workspace)")
    return 0


def _try_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------ run


def _cmd_run(args: argparse.Namespace) -> int:
    script = Path(args.path).resolve()
    if not script.exists():
        print(f"error: script not found: {script}", file=sys.stderr)
        return 2
    # 决定 sink 路径
    if args.sink:
        sink_path = Path(args.sink).resolve()
    else:
        import time as _t

        sink_dir = script.parent / ".redpymake" / "runs"
        sink_dir.mkdir(parents=True, exist_ok=True)
        sink_path = sink_dir / f"{script.stem}-{int(_t.time() * 1000)}.ndjson"
    sink_path.parent.mkdir(parents=True, exist_ok=True)

    # 提前 AST 检测：无 rpm.script(...) 块时打个告警
    try:
        card_list = discover(script.parent, patterns=[script.name])
        matched: ScriptCard | None = None
        for c in card_list:
            try:
                if Path(c.path).resolve() == script:
                    matched = c
                    break
            except Exception:  # pragma: no cover
                continue
        if matched is not None and not matched.has_script_block and matched.error is None:
            print(
                f"WARNING: {script.name} matches rpm_*.py but has no "
                f"'with rpm.script(...)' block (no_script_block)",
                file=sys.stderr,
            )
    except Exception:  # pragma: no cover - 告警检测失败不影响运行
        pass

    # 起子进程
    env = os.environ.copy()
    env["REDPYMAKE_LIVE_SINK"] = f"file://{sink_path.as_posix()}"
    print(f"[redpymake] sink: {sink_path}", file=sys.stderr)
    proc = subprocess.run([sys.executable, str(script)], env=env)
    return int(proc.returncode)


# ------------------------------------------------------------ report


def _cmd_report(args: argparse.Namespace) -> int:
    from ._web.report import render_report

    ndjson = Path(args.ndjson).resolve()
    if not ndjson.exists():
        print(f"error: ndjson not found: {ndjson}", file=sys.stderr)
        return 2
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    render_report(ndjson, out)
    print(f"[redpymake] wrote {out}")
    return 0


# ------------------------------------------------------------ serve


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        from ._web.server import build_app
    except ImportError as exc:
        print(
            "error: redpymake[web] extras not installed. "
            "run `pip install 'redpymake[web]'` first.\n"
            f"underlying: {exc}",
            file=sys.stderr,
        )
        return 2
    try:
        import uvicorn
    except ImportError:
        print(
            "error: uvicorn not installed. run `pip install 'redpymake[web]'` first.",
            file=sys.stderr,
        )
        return 2
    from . import workspace as _make_ws

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host}; do not expose this on public networks.",
            file=sys.stderr,
        )
    url = _build_serve_url(args.host, args.port)
    should_open = _should_open_browser(args.open_browser)
    # 每次 serve 都是一次独立的"录制会话"：默认另起一份日志，上一份为空则复用
    with _make_ws(args.root, new_log_on_start=not args.resume_log) as ws:
        app = build_app(ws)
        if should_open:
            _schedule_open_browser(args.host, args.port, url)
        else:
            print(f"[redpymake] serving at {url}", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ------------------------------------------------------------ logs


def _cmd_logs_list(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        logs = ws.list_logs()
        active_id = ws.current_log.id
    if args.json:
        print(json.dumps([log.to_dict() for log in logs], ensure_ascii=False, indent=2))
        return 0
    if not logs:
        print("(no logs)")
        return 0
    for log in logs:
        marks = []
        if log.is_active:
            marks.append("active")
        if log.pinned:
            marks.append("pinned")
        tag = f"  [{', '.join(marks)}]" if marks else ""
        line = f"{log.id}  {log.name}  ({log.run_count} runs){tag}"
        try:
            print(line)
        except UnicodeEncodeError:  # pragma: no cover
            sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    _ = active_id  # 兼容旧接口用途
    return 0


def _cmd_logs_rename(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            log = ws.rename_log(args.log_id, args.name, description=args.description)
        except KeyError:
            print(f"error: log not found: {args.log_id}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(f"renamed: {log.id}  ->  {log.name}")
    return 0


def _cmd_logs_rotate(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            log = ws.rotate_log(name=args.name)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    print(f"new active log: {log.id}  ({log.name})")
    return 0


def _cmd_logs_pin(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            log = ws.pin_log(args.log_id, pinned=args.pin)
        except KeyError:
            print(f"error: log not found: {args.log_id}", file=sys.stderr)
            return 2
    print(f"{'pinned' if log.pinned else 'unpinned'}: {log.id}")
    return 0


def _cmd_logs_discard(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            ws.discard_log(args.log_id)
        except KeyError:
            print(f"error: log not found: {args.log_id}", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    print(f"discarded: {args.log_id}")
    return 0


# ------------------------------------------------------------ helpers


def _build_serve_url(host: str, port: int) -> str:
    """把 ``--host --port`` 拼成人类可点开的 URL。

    ``0.0.0.0`` / ``::`` 这种"监听所有网卡"的地址浏览器无法直连，替换成
    ``127.0.0.1``；IPv6 字面量用中括号包起来。
    """
    display_host = host
    if host in ("0.0.0.0", "", "::"):
        display_host = "127.0.0.1"
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def _should_open_browser(flag: bool | None) -> bool:
    """决定是否触发浏览器打开。

    优先级：CLI ``--no-open`` > 环境变量 ``REDPYMAKE_NO_OPEN=1`` > 默认开启。
    显式 ``--open`` 会强制忽略环境变量。
    """
    if flag is False:
        return False
    if flag is True:
        return True
    env_flag = os.environ.get("REDPYMAKE_NO_OPEN", "").strip().lower()
    if env_flag in ("1", "true", "yes", "on"):
        return False
    return True


def _schedule_open_browser(host: str, port: int, url: str) -> threading.Thread:
    """启动一个后台线程：等端口可连通后再调 ``webbrowser.open``。

    - 只在 uvicorn 起来后打开，避免"页面 404 / 连接失败"闪一下；
    - 超时 8 秒还没通就静默放弃（不影响 serve 主循环）；
    - Windows 上 ``webbrowser.open`` 会走系统默认浏览器（用户在"默认应用"
      里设的那一个）；BROWSER 环境变量会覆盖该默认。
    """

    def _worker() -> None:
        target_host = host if host not in ("0.0.0.0", "", "::") else "127.0.0.1"
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((target_host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.15)
        else:
            print(
                f"[redpymake] serve is up but port never accepted; skip opening browser.\n"
                f"open manually: {url}",
                file=sys.stderr,
            )
            return
        try:
            print(f"[redpymake] opening {url} in default browser ...", file=sys.stderr)
            webbrowser.open(url, new=2, autoraise=True)
        except Exception as exc:  # pragma: no cover - 各平台差异
            print(
                f"[redpymake] failed to open browser ({exc!r}); open manually: {url}",
                file=sys.stderr,
            )

    t = threading.Thread(target=_worker, name="rpm-open-browser", daemon=True)
    t.start()
    return t


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
