# RedPyMake — Agent Guide

Cross-environment automation library (local / ssh / adb / serial / wsl) behind one
`Session` interface: `run` / `at` / `path` / `push` / `pull` / `wait` / `logs`.
Python 3.10+, zero core dependencies, PEP 561 typed.

## Imports

`import redpymake as rpm`. Use only names in `rpm.__all__`; underscore modules
have no stability guarantee. SSH / serial are optional extras exposed lazily via
`__init__.__getattr__` — do not import `paramiko` / `pyserial` at module scope.

## Five counter-intuitive semantics

Where `subprocess` / `paramiko` habits produce code that compiles, reads
correctly, and is wrong.

1. **`run()` defaults to `check=True`.** Non-zero exit raises `CommandError`
   immediately. To inspect the exit code yourself, pass `check=False` and read
   `res.ok` / `res.rc`. `if sess.run(cmd).returncode != 0:` is unreachable.

2. **`at()` returns a new view; it does not mutate.** After
   `build = sess.at("/workspace")`, `sess` still has its original cwd. The view
   shares connection and log buffer. For a single-call override use
   `run(..., cwd=...)`. There is no mutable `session.pwd`.

3. **`shell=False` (default) doesn't parse `|` `&&` `>` or expand `~` / `$VAR` /
   globs.** Positional args are literal and `shlex.quote`-protected on remote
   sessions. For shell syntax use `run("a | b", shell=True)` — extra positional
   args then raise `TypeError`. To expand `~`, use `sess.path("~/x")`.

4. **`wait()` scans forward from the call site.** It will not match output that
   already arrived. For output produced *during* a command, use
   `sess.run(cmd).wait(pattern)` (searches from the pre-run cursor). OR is
   `wait(["A", "B"])` — `wait("A", "B")`'s second positional is `timeout`.
   Chain `.wait().wait()` for ordered matches.

5. **Serial `run()` appends no line terminator.** Write `uart.run("reboot\r")`
   or construct with `rpm.serial(port, newline="\r")`. `bytes` is serial-only;
   elsewhere → `TypeError`.

## Error handling

Failure is part of the interface. Raise immediately with `raise ... from exc`.

Forbidden: `except Exception` followed by `return None`, `pass`, a silent retry,
or recording the failure as an empty value and continuing.

Everything crossing the public boundary derives from `RedPyMakeError`. Paramiko
exceptions, bare `RuntimeError`, and `NotImplementedError` must not leak — wrap
them (`UnsupportedOperationError` substitutes for unsupported platform
capabilities).

The library never calls `logging.basicConfig()`. Session output goes through
`SessionLogs`.

## Conventions

Implementation details prefixed `_`. Structured types are dataclasses,
`frozen=True` preferred. Internals use relative imports.

## Workflow for changing code

`doc/core-lib-requirements.md` is the single source of truth; behavior disputes
resolve against it. Test-first is enforced — see `.cursor/rules/test-first.mdc`
for the full contract. Essentials:

- Changing a requirement means changing the document **and** the tests first.
- Any change under `src/redpymake/` starts with a failing test in the matching
  `tests/test_coreXX_*.py`; each test docstring cites `§CORE-XX/...`.
- Tests exercise real calls (`session.run(...)`), not internals like
  `sess.logs.buffer.append(...)`.

## Verification

```bash
pytest                    # default suite
pytest --cov=redpymake    # 80% gate
```

`integration` (real devices, needs `RPM_TEST_*`) and `e2e` (needs
`playwright install chromium`) are excluded by `addopts` to keep the default
suite hermetic (§5/7). 30s per-test timeout prevents hung `wait()` / subprocess
from stalling CI.

## Further reading

- `doc/api-cheatsheet.md` — signature reference
- `readme.md` — installation, examples, pitfalls
- `doc/core-lib-requirements.md` — full specification (~1600 lines)
- `doc/TODO.md` — roadmap
