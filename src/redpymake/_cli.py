"""顶层 CLI (CORE-10 / CORE-11)。

- ``redpymake discover [ROOT] [--json]``：AST 发现，输出树或 JSON；
- ``redpymake run PATH [--sink FILE]``：子进程运行单个脚本，环境变量注入
  ``REDPYMAKE_LIVE_SINK`` 使 CORE-10 sink 生效；透传脚本 exit code；
- ``redpymake targets [--root] [--json]``：列出命名目标，不建连；
- ``redpymake exec [选项] TARGET [CMD ...]``：在命名目标上跑一条命令；
- ``redpymake cp [选项] SOURCE DEST``：复制本机或 ``@目标:路径`` 中的文件；
- ``redpymake report NDJSON -o OUT.html``：从 NDJSON 生成自包含 HTML 报告；
- ``redpymake serve [ROOT] [--host --port]``：起 FastAPI Web UI（依赖 ``[web]`` extra）；
  每次启动另起一份活跃日志（上一份为空则复用），``--resume-log`` 可续用上次那份。

本地侧失败的 stderr 以 ``redpymake: `` 开头。退出码见 CORE-10。

同时提供 ``python -m redpymake`` 入口（见 ``__main__.py``）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

from ._discover import ScriptCard, discover
from ._factory import local
from ._path import ResourcePath
from ._session import Session, copy
from ._targets import legal_target_spec, list_targets, target
from .exceptions import (
    CommandTimeoutError,
    RedPyMakeError,
    ResourceNotFoundError,
    SessionConnectionError,
    TargetConfigError,
    TransferError,
    UnsupportedOperationError,
)

# sysexits。3 是日志组状态冲突的既有码，不并进这组。
EX_USAGE = 64
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_IOERR = 74
EX_CONFIG = 78
EX_TIMEOUT = 124
EX_CONFLICT = 3


def _write_text(text: str, stream: TextIO) -> None:
    """Keep a text stream's encoding; escape only unrepresentable characters."""
    encoding = getattr(stream, "encoding", None)
    if encoding:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    stream.write(text)


def _print(text: str, *, file: TextIO | None = None) -> None:
    _write_text(text + "\n", sys.stdout if file is None else file)


class _Parser(argparse.ArgumentParser):
    """用法错误统一退出 64，诊断带 ``redpymake: `` 前缀。"""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"redpymake: {message}\n")

    def _print_message(self, message, file=None):
        if message:
            _write_text(message, sys.stderr if file is None else file)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(
        prog="redpymake",
        description="RedPyMake 脚本发现 / 运行 / 可视化 CLI",
    )
    subs = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

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

    # targets
    p_targets = subs.add_parser("targets", help="列出命名目标（不建连）")
    p_targets.add_argument("--root", default=".", help="查找项目根的起点（默认当前目录）")
    p_targets.add_argument("--json", action="store_true", help="输出 JSON 列表")
    p_targets.set_defaults(func=_cmd_targets)

    # exec。选项必须写在 TARGET 前面：CMD 用 remainder，后面的 flag 会变成远端参数。
    p_exec = subs.add_parser(
        "exec",
        help="在命名目标上执行一条命令（选项必须写在目标名前面）",
    )
    p_exec.add_argument("--cwd", default=None, help="只覆盖这一次命令的工作目录")
    p_exec.add_argument("--shell", action="store_true", help="把命令交给远端 shell")
    p_exec.add_argument("--timeout", type=float, default=None, help="秒；超时退出 124")
    p_exec.add_argument("--root", default=".", help="查找项目根的起点（默认当前目录）")
    p_exec.add_argument("target", help="目标名，board 与 @board 等价")
    p_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令")
    p_exec.set_defaults(func=_cmd_exec)

    # cp。只有显式 @NAME:PATH 才是目标引用，避免与 Windows 盘符冲突。
    p_cp = subs.add_parser("cp", help="复制单个文件（本机路径或 @目标:路径）")
    p_cp.add_argument("--root", default=".", help="查找项目根的起点（默认当前目录）")
    p_cp.add_argument(
        "--timeout", type=_cp_timeout, default=None, help="传输超时秒数（有限正数）",
    )
    p_cp.add_argument(
        "-n", "--no-clobber", dest="overwrite", action="store_false",
        help="跳过已有目标文件（默认覆盖）",
    )
    p_cp.add_argument("source", type=_cp_endpoint, help="源文件：本机路径或 @目标:路径")
    p_cp.add_argument("dest", type=_cp_endpoint, help="目标文件路径或已有目录")
    p_cp.set_defaults(func=_cmd_cp)

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
        _print(json.dumps([c.to_dict() for c in cards], ensure_ascii=True, indent=2))
        return 0
    if not cards:
        _print(f"(no rpm_*.py under {root})")
        return 0
    for card in cards:
        rel = card.path.relative_to(root) if _try_relative(card.path, root) else card.path
        prefix = "[ok]" if card.has_script_block else "[warn]"
        name = card.script_name or "(unnamed)"
        factories = ",".join(card.factories) if card.factories else "-"
        line = f"{prefix} {rel}    name={name}  factories=[{factories}]"
        _print(line)
        if card.error:
            _print(f"    error: {card.error}")
        if not card.has_script_block and not card.error:
            _print(f"    (no rpm.script(...) block; will not be run by workspace)")
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
        return _die(EX_NOINPUT, f"script not found: {script}")
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
            _print(
                f"WARNING: {script.name} matches rpm_*.py but has no "
                f"'with rpm.script(...)' block (no_script_block)",
                file=sys.stderr,
            )
    except Exception:  # pragma: no cover - 告警检测失败不影响运行
        pass

    # 起子进程
    env = os.environ.copy()
    env["REDPYMAKE_LIVE_SINK"] = f"file://{sink_path.as_posix()}"
    env["REDPYMAKE_CLI"] = "1"
    _print(f"[redpymake] sink: {sink_path}", file=sys.stderr)
    proc = subprocess.run([sys.executable, str(script)], env=env)
    return int(proc.returncode)


# ------------------------------------------------------------ targets / exec


def _cmd_targets(args: argparse.Namespace) -> int:
    try:
        infos = list_targets(args.root)
    except TargetConfigError as exc:
        return _die(EX_CONFIG, str(exc))
    if args.json:
        payload = [info.to_dict() for info in infos]
        _print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    if not infos:
        return 0
    for info in infos:
        summary = " ".join(f"{key}={info.params[key]}" for key in sorted(info.params))
        src = " + ".join(info.source)
        line = f"{info.name}  {info.kind}  {summary}  ({src})".rstrip()
        _print(line)
    return 0


def _cmd_exec(args: argparse.Namespace) -> int:
    if not legal_target_spec(args.target):
        return _die(EX_USAGE, f"illegal target name {args.target!r}")
    cmd = list(args.cmd)
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if args.shell and not cmd:
        return _die(EX_USAGE, "--shell requires a command")
    try:
        sess = target(args.target, root=args.root)
    except TargetConfigError as exc:
        return _die(EX_CONFIG, str(exc))
    except (SessionConnectionError, UnsupportedOperationError) as exc:
        return _die(EX_UNAVAILABLE, str(exc))
    with sess:
        if not cmd:
            return 0
        try:
            if args.shell:
                res = sess.run(
                    " ".join(cmd),
                    shell=True,
                    cwd=args.cwd,
                    timeout=args.timeout,
                    check=False,
                )
            else:
                res = sess.run(
                    cmd[0],
                    *cmd[1:],
                    cwd=args.cwd,
                    timeout=args.timeout,
                    check=False,
                )
        except CommandTimeoutError as exc:
            _write_streams(exc.stdout, exc.stderr)
            return _die(EX_TIMEOUT, str(exc))
        except (SessionConnectionError, UnsupportedOperationError) as exc:
            return _die(EX_UNAVAILABLE, str(exc))
        _write_streams(res.stdout, res.stderr)
        return int(res.returncode)


def _write_streams(stdout: str, stderr: str) -> None:
    if stdout:
        _write_text(stdout, sys.stdout)
    if stderr:
        _write_text(stderr, sys.stderr)


# ------------------------------------------------------------ cp


@dataclass(frozen=True)
class _CopyEndpoint:
    name: str | None
    path: str


def _cp_endpoint(value: str) -> _CopyEndpoint:
    if not value:
        raise argparse.ArgumentTypeError("copy path must not be empty")
    if not value.startswith("@"):
        return _CopyEndpoint(None, value)
    spec, separator, path = value.partition(":")
    if not separator or not path or not legal_target_spec(spec):
        raise argparse.ArgumentTypeError(
            f"invalid copy endpoint {value!r}; expected @NAME:PATH "
            "(use ./@file for a local filename starting with @)"
        )
    return _CopyEndpoint(spec[1:], path)


def _cp_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("--timeout must be a finite positive number")
    return timeout


def _cmd_cp(args: argparse.Namespace) -> int:
    try:
        with ExitStack() as stack:
            sessions: dict[str | None, Session] = {}
            paths: list[ResourcePath] = []
            for endpoint in (args.source, args.dest):
                if endpoint.name not in sessions:
                    sess = (
                        local() if endpoint.name is None
                        else target(endpoint.name, root=args.root)
                    )
                    sessions[endpoint.name] = stack.enter_context(sess)
                paths.append(sessions[endpoint.name].path(endpoint.path))
            source, dest = paths
            if source.stat().is_dir:
                raise UnsupportedOperationError("cp does not support directory sources")
            if dest.is_dir():
                # 文件名是字面的；避免 ~ 开头的名字被再次当成家目录展开。
                dest = dest.session.at(dest.path).path("./" + source.name)
            copy(source, dest, overwrite=args.overwrite, timeout=args.timeout)
    except (RedPyMakeError, OSError) as exc:
        return _die(_cp_error_code(exc), str(exc))
    return 0


def _cp_error_code(exc: BaseException) -> int:
    # CORE-04 包裹后端异常；仍按已知根因区分缺文件、超时和连接失败。
    while isinstance(exc, TransferError):
        cause = exc.cause or exc.__cause__
        if cause is None:
            break
        exc = cause
    if isinstance(exc, TargetConfigError):
        return EX_CONFIG
    if isinstance(exc, (ResourceNotFoundError, FileNotFoundError)):
        return EX_NOINPUT
    if isinstance(exc, (CommandTimeoutError, TimeoutError)):
        return EX_TIMEOUT
    if isinstance(exc, (SessionConnectionError, UnsupportedOperationError)):
        return EX_UNAVAILABLE
    return EX_IOERR


def _die(code: int, message: str) -> int:
    _print(f"redpymake: {message}", file=sys.stderr)
    return code


# ------------------------------------------------------------ report


def _cmd_report(args: argparse.Namespace) -> int:
    from ._web.report import render_report

    ndjson = Path(args.ndjson).resolve()
    if not ndjson.exists():
        return _die(EX_NOINPUT, f"ndjson not found: {ndjson}")
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    render_report(ndjson, out)
    _print(f"[redpymake] wrote {out}")
    return 0


# ------------------------------------------------------------ serve


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        from ._web.server import build_app
    except ImportError as exc:
        return _die(
            EX_UNAVAILABLE,
            "redpymake[web] extras not installed. "
            "run `pip install 'redpymake[web]'` first.\n"
            f"underlying: {exc}",
        )
    try:
        import uvicorn
    except ImportError:
        return _die(
            EX_UNAVAILABLE,
            "uvicorn not installed. run `pip install 'redpymake[web]'` first.",
        )
    from . import workspace as _make_ws

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        _print(
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
            _print(f"[redpymake] serving at {url}", file=sys.stderr)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ------------------------------------------------------------ logs


def _cmd_logs_list(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        logs = ws.list_logs()
        active_id = ws.current_log.id
    if args.json:
        _print(json.dumps([log.to_dict() for log in logs], ensure_ascii=True, indent=2))
        return 0
    if not logs:
        _print("(no logs)")
        return 0
    for log in logs:
        marks = []
        if log.is_active:
            marks.append("active")
        if log.pinned:
            marks.append("pinned")
        tag = f"  [{', '.join(marks)}]" if marks else ""
        line = f"{log.id}  {log.name}  ({log.run_count} runs){tag}"
        _print(line)
    _ = active_id  # 兼容旧接口用途
    return 0


def _cmd_logs_rename(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            log = ws.rename_log(args.log_id, args.name, description=args.description)
        except KeyError:
            return _die(EX_NOINPUT, f"log not found: {args.log_id}")
        except ValueError as exc:
            return _die(EX_USAGE, str(exc))
    _print(f"renamed: {log.id}  ->  {log.name}")
    return 0


def _cmd_logs_rotate(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            log = ws.rotate_log(name=args.name)
        except RuntimeError as exc:
            return _die(EX_CONFLICT, str(exc))
    _print(f"new active log: {log.id}  ({log.name})")
    return 0


def _cmd_logs_pin(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            log = ws.pin_log(args.log_id, pinned=args.pin)
        except KeyError:
            return _die(EX_NOINPUT, f"log not found: {args.log_id}")
    _print(f"{'pinned' if log.pinned else 'unpinned'}: {log.id}")
    return 0


def _cmd_logs_discard(args: argparse.Namespace) -> int:
    from . import workspace as _make_ws

    with _make_ws(args.root) as ws:
        try:
            ws.discard_log(args.log_id)
        except KeyError:
            return _die(EX_NOINPUT, f"log not found: {args.log_id}")
        except RuntimeError as exc:
            return _die(EX_CONFLICT, str(exc))
    _print(f"discarded: {args.log_id}")
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
            _print(
                f"[redpymake] serve is up but port never accepted; skip opening browser.\n"
                f"open manually: {url}",
                file=sys.stderr,
            )
            return
        try:
            _print(f"[redpymake] opening {url} in default browser ...", file=sys.stderr)
            webbrowser.open(url, new=2, autoraise=True)
        except Exception as exc:  # pragma: no cover - 各平台差异
            _print(
                f"[redpymake] failed to open browser ({exc!r}); open manually: {url}",
                file=sys.stderr,
            )

    t = threading.Thread(target=_worker, name="rpm-open-browser", daemon=True)
    t.start()
    return t


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
