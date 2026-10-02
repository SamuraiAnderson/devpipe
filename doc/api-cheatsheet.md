# RedPyMake API Cheatsheet

Signature reference transcribed from `src/redpymake/` as of v0.6.0 — what the
code does today. What it *should* do is defined by
`doc/core-lib-requirements.md`; a disagreement is a bug in one of them.

Only names exported from `rpm` are public (`import redpymake as rpm`).

## Factories

All return the same `Session` interface. SSH/ADB/serial connect eagerly and raise
`SessionConnectionError` on failure; WSL only checks that `wsl.exe` is executable
and defers distro failures to the first `run()` as a `CommandError`.

| Signature | Requires |
| --- | --- |
| `rpm.local(*, default_cwd=None) -> Session` | — |
| `rpm.ssh(host, *, user=None, port=22, password=None, key_filename=None, default_cwd=None, connect_timeout=15.0, platform="auto") -> Session` | `[ssh]` (paramiko) |
| `rpm.adb(serial=None, *, adb_path=None, default_cwd=None, connect_timeout=10.0) -> Session` | `adb` on PATH |
| `rpm.serial(port, *, baudrate=115200, newline="", encoding="utf-8") -> Session` | `[serial]` (pyserial) |
| `rpm.wsl(distribution=None, *, user=None, wsl_path=None, default_cwd=None) -> Session` | Windows |

Inside a `rpm.workspace(...)` scope these factories transparently borrow a pooled
session; the script source needs no change. `default_cwd` then yields an `at()`
view over the shared connection, while the other arguments must match what the
pool was built with — see `Workspace` below.

## Session

SSH sessions can create a device session on the SSH host:

```python
session.adb(serial=None, *, adb_path=None, default_cwd=None, connect_timeout=10.0)
session.wsl(distribution=None, *, user=None, wsl_path=None, default_cwd=None)
session.serial(port, *, baudrate=115200, newline="", encoding="utf-8",
               python_path=None, connect_timeout=15.0)
```

These return the endpoint's Session implementation with independent logs and paths.
Only SSH hosts are supported in this release; WSL requires a Windows SSH host.
`platform="auto"` probes POSIX/Windows; explicit values are `"posix"` and `"windows"`.
Remote UART needs Python 3.10+ with pyserial; no persistent RedPyMake installation.
The bridge preserves bytes/order, not timing or UART control signals. Its client
lease expires after `max(5, 2 * connect_timeout)` seconds without control frames.

Closing an exclusively chained leaf closes its upstream connection. An outer
`with host:` scope or Workspace retains ownership, permitting multiple children.
Closing the parent closes children first. `at()` remains a view of the endpoint;
the host view's cwd is never inherited. Workspace keys include upstream identity.
Unexpected read/disconnect failures wake waits with `SessionConnectionError`;
`SessionError` reports overwritten wait data. Unconfirmed operations are not replayed.

Context manager. `close()` is idempotent; operations after close raise
`SessionClosedError` but collected logs remain readable.

```python
session.at(path)   -> Session        # new view bound to a new cwd; shares connection + logs
session.path(raw)  -> ResourcePath   # relative paths resolve against this view's cwd
session.close()    -> None
```

```python
session.run(
    command: str | bytes,
    *args: str,
    shell: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
    encoding: str = "utf-8",
    log_command: bool = True,
) -> CommandResult
```

Working-directory precedence: `run(cwd=...)` > `at()` > session default.
`shell=True` takes exactly one command string and rejects extra positional args
(`TypeError`). `bytes` is serial-only: written verbatim, no `newline` appended,
no `*args`, no `shell=True`.

```python
session.wait(
    pattern: str | bytes | re.Pattern | Sequence[...],
    timeout: float = 30,
    *,
    channel: str | None = None,     # "stdout" | "stderr" | "serial" | "system"
    since: LogCursor | None = None,
    multiline: bool = False,
) -> LogMatch
```

List/tuple → OR, first hit wins (`match.index` tells which). Mixing `str` and
`bytes` → `TypeError`; empty sequence → `ValueError`. `multiline=True` joins
records with `\n` before searching (text only). `bytes` patterns search the raw
serial RX ring buffer and are not split on newlines; passing `channel` /
`multiline` / `since` alongside them raises `TypeError`. Timeout →
`LogWaitTimeoutError`.

```python
session.push(source, target, *, overwrite=True, timeout=None) -> TransferResult
session.pull(source, target, *, overwrite=True, timeout=None) -> TransferResult
```

`push` moves *into* the calling session, `pull` moves *out of* it. The
session-bound end must belong to the caller; the other end may be a bare string
(local path) or any `ResourcePath`.

Properties: `session_id`, `kind`, `label`, `logs`, `default_cwd`, `path_style`
(`"posix"` or `"windows"`), `home_dir`, `closed`, `is_view`, `root`.

`home_dir` is `None` until a `~` expansion probes it — reading the property never
triggers the probe. Use `session.path("~")` when you need the value.

## Top-level functions

```python
rpm.copy(source, target, *, overwrite=True, timeout=None) -> TransferResult
```

Bare strings / `os.PathLike` bind to the process-wide default local session —
they do *not* follow `rpm.local(default_cwd=...)` or an `at()` view.
Remote-to-remote relays through a local temp file automatically.

```python
rpm.stale(target, depends_on=(), *, strategy="mtime", name=None) -> bool
```

`True` ⇒ target out of date. Targets/dependencies: single `PathSpec`
(`str | os.PathLike | ResourcePath`) or iterable. Missing dependency →
`InputNotFoundError` (never treated as stale). Missing target → `True`.
`strategy="hash"` → `UnsupportedOperationError`. Custom callable must match
`StalePredicate: (targets, sources) -> bool`. Emits one `stale.check` log record
per call.

```python
rpm.script(
    name=None, *,
    dump_on_error=None,      # None|False off; path with suffix -> single file;
                             # other path -> directory bundle; callable -> sink
    log_level="INFO",
    loggers=None,            # opt-in allowlist; default hooks the root logger
    console=None,            # add a root StreamHandler; None -> REDPYMAKE_CLI=1
) -> ScriptRun
```

Context manager only. Sessions constructed inside auto-register. On an unhandled
exception, *if `dump_on_error` is set*: dumps merged `logging` + every registered
session's log, then re-raises. Dump failure never masks the original. With the
default `dump_on_error=None` nothing is written.

`console` never calls `logging.basicConfig()`. When it resolves true, the run
attaches its own root `StreamHandler` — unless the root logger already has one
(`FileHandler` counts, it is a subclass) — and lowers the root level to
`log_level` if the root is more restrictive or unset. On exit the handler is
removed and the level restored, so nothing of the host's logging setup changes.

```python
rpm.discover(root=".", *, patterns=None, exclude=None) -> list[ScriptCard]
```

Pure AST analysis of files matching `rpm_*.py`; never imports or executes them.
Syntax errors become cards with `error` set, don't crash the scan.
`patterns=None` reads `[tool.redpymake.discovery]` from the nearest
`pyproject.toml`. Output sorted by path.

```python
rpm.target(name, *, root=".", **overrides) -> Session
```

Resolves `name` (`board` and `@board` are the same) from
`[tool.redpymake.targets.<name>]` overlaid key-by-key with
`<project>/.redpymake/targets.toml`. The project root is the nearest
`pyproject.toml` within six directories; without one, the nearest
`targets.toml`. Forwards the merged keys to the matching factory, so a
workspace pool is borrowed automatically. Literal `password` in either file
raises `TargetConfigError`; set `password_env` to an environment variable
name instead. `**overrides` win, and may pass `password`. `~` in
`key_filename` is expanded.

```python
rpm.workspace(
    root=".", *,
    logs_root=None,              # default <root>/.redpymake/logs/
    ndjson_dir=None,             # legacy alias for logs_root
    discovery_patterns=None,
    auto_close_sessions=True,
    log_name=None,
    new_log_on_start=False,
) -> Workspace
```

Context manager only. Owns a session pool reused across scripts, a serial queue,
and named log groups.

## Data objects

### `CommandResult` (frozen)

| Field | Type |
| --- | --- |
| `command` | `tuple[str \| bytes, ...]` |
| `returncode` | `int` |
| `stdout` / `stderr` | `str` |
| `duration` | `float` (seconds) |
| `session` | `Session` |

`ok -> bool` (exit code 0), `rc -> int` (alias for `returncode`),
`raise_for_status() -> None`, and
`wait(pattern, timeout=30, *, channel=None, multiline=False) -> LogMatch`, which
searches from the cursor captured *before* the command ran, so output produced
during execution is never missed.

### `TransferResult` (frozen)

`source: ResourcePath`, `target: ResourcePath`, `transferred: bool`,
`bytes_transferred: int`, `duration: float`. `transferred=False` means the copy
was skipped (e.g. `overwrite=False` hit an existing target).

### `ResourcePath` / `ResourceStat`

Created via `session.path(...)`; bound to its session (cross-device deps work).
Relative paths frozen against the creating view.

Props: `session`, `path` (resolved), `raw` (as given), `name`, `parent`. Methods:
`exists()`, `is_file()`, `is_dir()`, `stat() -> ResourceStat`,
`remove(*, missing_ok=False)`, `mkdir(*, parents=False, exist_ok=False)`,
`refresh()` (reserved; currently no-op). `~` / `~/...` expand to session home;
`~user` → `UnsupportedOperationError`.

`ResourceStat` (frozen): `size: int`, `mtime: float`, `is_dir: bool`.

### `SessionLogs` (`session.logs`)

```python
logs.records(*, since=None, channel=None) -> list[SessionLogRecord]
logs.text(*, since=None, channel=None, include_events=("command_output",)) -> str
logs.save(path, *, channel=None, include_events=("command_output",)) -> None
logs.cursor() -> LogCursor
logs.subscribe(callback) -> Callable[[], None]   # returns unsubscribe
with logs.tag(**fields): ...                     # nest-able; inner keys win
```

Views share the root buffer. Thread-safe, bounded, readable after close.
Passwords and `env` values are not recorded.

### `SessionLogRecord` (frozen)

`timestamp: float`, `sequence: int`, `session_id: str`, `event: str`,
`level: str`, `stream: str` (`stdout` / `stderr` / `serial` / `system` /
`python`), `message: str`, `operation_id: str | None`, `fields: Mapping[str, Any]`.

`operation_id` ties a command's start/output/end. Also the NDJSON live sink's
wire format.

### `LogCursor` / `LogMatch` (frozen)

`LogCursor`: `session_id`, `sequence`.

`LogMatch`: `pattern`, `record` (`None` for byte matches), `text` (`str` or
matched `bytes`), `elapsed`, `index` (OR position, `0` otherwise),
`command_result`. `.wait(...)` continues from just after this hit.

### `ScriptCard` (frozen)

`path: Path`, `module_name: str`, `script_name: str | None` (only when
`rpm.script(name=...)` is a literal), `docstring: str | None`,
`factories: tuple[str, ...]`, `has_script_block: bool`, `lineno: int | None`,
`error: str | None`.

### `ScriptRun` / `ScriptSnapshot`

`ScriptRun`: `name`, `started_at`, `ended_at`, `active`; `attach(session)`,
`detach(session)`, `snapshot(exception=None) -> ScriptSnapshot`. Dedupes by
`session_id`.

`ScriptSnapshot` (frozen): `name`, `started_at`, `ended_at`,
`records: tuple[SessionLogRecord, ...]` (sorted by
`(timestamp, session_id, sequence)`), `sessions: tuple[SessionInfo, ...]`
(`{id, kind, label}`), `exception: ExceptionInfo | None`
(`{type, message, traceback}`).

### `Workspace`

Session pool (keyed, reused): `local()`, `ssh(host, ...)`, `adb(serial=None)`,
`serial(port, ...)`, `wsl(distribution=None, ...)`, `sessions()`,
`get_session_by_id(id)`.

Keys are `"local"`, `"ssh:<user>@<host>:<port>"`, `"adb:<serial>"`,
`"serial:<port>@<baudrate>"`, `"wsl:<distribution>"` (`default` substitutes for
`None`). On a key hit the pooled connection is reused, and the arguments outside
the key must agree or you get `ValueError` — `password`, `key_filename`,
`connect_timeout`, `adb_path`, `newline`, `encoding`, and WSL's `user` (which is
*not* part of the key). The message names the conflicting parameters without
their values.

`default_cwd` is the exception: it never conflicts. Pooled sessions are built
without one, and each borrow with a `default_cwd` hands back an `at()` view, so
two scripts can ask for different working directories over the same connection.

Queue (one worker, serial): `enqueue(path) -> run_id`, `cancel_run(id)`,
`rerun(id)`, `stop_current()`, `pause_queue()`, `resume_queue()`,
`clear_queue()`, `current_run`, `runs`, `get_run(id)`, `iter_run_records(id)`.
Scripts must expose callable `main`; modules are `importlib.reload`-ed each run.

Log groups: `current_log`, `list_logs()`, `get_log(id)`, `rename_log(...)`,
`rotate_log(name=None)`, `pin_log(id, pinned=True)`, `discard_log(id)`,
`list_runs_in_log(id)`.

`rotate_log` raises `RuntimeError` outside the workspace context, while a run is
in progress, or while any run is still queued — a paused queue with pending
items blocks it too. `discard_log` refuses the active log and pinned logs
(`RuntimeError`); `rename_log` rejects a blank name (`ValueError`); an unknown
log id is `KeyError` throughout.

Also `discover()`, `refresh()`, `subscribe(callback)`, `close()`.

`WorkspaceRun` (frozen): `id`, `script_path`, `script_name`, `status`
(`queued` / `running` / `succeeded` / `failed` / `cancelled` / `interrupted`),
`started_at`, `ended_at`, `exception`, `ndjson_path` (log group's shared
`stream.ndjson`, *not* per-run), `snapshot`, `log_id`, `stream_offset_begin`,
`stream_offset_end`.

`WorkspaceLog` (frozen): `id`, `name`, `created_at`, `description`, `pinned`,
`root`, `run_count`, `is_active`, `stream_path`, `sessions_dir`.

`stop_current()` is cooperative: borrowed sessions check the flag before each
`run()` / `wait()` / `push()` / `pull()` and raise `WorkspaceStoppedError` — the
script stops at the *next* command boundary. Already-running children are not
killed.

## Exceptions

```text
RedPyMakeError
├── SessionError                 .session
│   ├── SessionConnectionError   .host .cause
│   └── SessionClosedError
├── CommandError                 .session .command .returncode .stdout .stderr
│   └── CommandTimeoutError      .timeout
├── TransferError                .source .target .cause .returncode .stdout .stderr
├── ResourceError                .path
│   ├── ResourceNotFoundError
│   └── InputNotFoundError       .name
├── LogWaitTimeoutError          .pattern .timeout .records .output .command_result .data
├── UnsupportedOperationError
├── WorkspaceStoppedError        .run_id
└── TargetConfigError            .name .source
```

Parse fields, not messages. `TransferError.stderr` carries the underlying shell
error when the failure came from an internal `cp` / `stat` / `rm` / `mkdir` probe.

## CLI and environment

```bash
redpymake discover [ROOT] [--json]
redpymake run PATH [--sink FILE]
redpymake targets [--root DIR] [--json]
redpymake exec [--cwd DIR] [--shell] [--timeout S] [--root DIR] TARGET [CMD ...]
redpymake report NDJSON -o report.html
redpymake serve [ROOT] [--host H] [--port P] [--no-open] [--resume-log]
redpymake logs list [ROOT] [--json]
redpymake logs rename [ROOT] LOG_ID NAME [--description D]
redpymake logs rotate [ROOT] [--name N]
redpymake logs pin [ROOT] LOG_ID [--no-pin]
redpymake logs discard [ROOT] LOG_ID
```

- `REDPYMAKE_LIVE_SINK=file:///abs/path.ndjson` — `ScriptRun` appends every
  `SessionLogRecord` as NDJSON, bracketed by `script.begin` / `script.end`,
  mirrored per-session into `<dir>/sessions/<slug>.ndjson`.
- `REDPYMAKE_CLI=1` — the default for `rpm.script(console=...)`; see the
  `console` note above. Set by `redpymake run` for the child process.
- `REDPYMAKE_NO_OPEN=1` — equivalent to `serve --no-open`.

`targets` lists merged targets and does not connect. `--json` is a list of
`{name, kind, source, params}`; `params` keeps `password_env` but never a
secret. `exec` options belong **before** `TARGET` — later flags are remote
arguments. No command means connect and exit 0. A command runs with
`check=False` and the remote returncode becomes the process exit code;
stdout/stderr are copied through unchanged and nothing is written to NDJSON.

Local failures print `redpymake: ...` on stderr: 64 usage, 66 missing input,
69 connection or missing extra, 78 `TargetConfigError`, 124 command timeout.
Log-group state conflicts stay on exit code 3.
