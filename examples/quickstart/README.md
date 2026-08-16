# 快速上手

一个文件，20 行代码，没有任何样板：[`hello_ssh.py`](hello_ssh.py)。

## 跑之前改三行

```python
HOST = "192.168.1.10"
USER = "root"
PASSWORD = "your-password"
```

然后：

```bash
pip install -e ".[ssh]"
python examples/quickstart/hello_ssh.py
```

预期输出是远程机器的 `uname -a` 结果，加上一行 `/tmp`。

## 手边没有 SSH 机器？

把工厂换成本机会话，其余代码一个字都不用改：

```python
with rpm.local() as remote:
    res = remote.run("python", "-c", "print('hi')")
    print(res.stdout.strip())
```

这正是这个库的核心：`rpm.local()` / `rpm.ssh(...)` / `rpm.wsl()` / `rpm.adb(...)` /
`rpm.serial(...)` 返回的都是同一套 `Session` 接口，下游的 `run` / `at` / `push` /
`wait` 写法完全一致。

## 为什么这个例子这么"素"

`examples/wsl_session/` 里的脚本都叫 `rpm_*.py`，还套着 `with rpm.script(...)`。
那些**都是可选的**，只在你想用 Web UI 或崩溃自动落盘时才需要：

- `rpm_*.py` 命名 → 让 `redpymake discover` / `redpymake serve` 能扫到并从浏览器触发；
- `with rpm.script(...)` → 把多个会话的日志和你的 `logging` 合流，出错时一键存现场。

平时写自动化脚本，像 `hello_ssh.py` 这样直接 `import redpymake as rpm` 就够了。
想让这个例子也出现在 Web UI 里，把它改名成 `rpm_hello_ssh.py` 即可。

## 下一步

| 想做的事 | 去哪看 |
| --- | --- |
| 传文件、跨平台路径 | [`../wsl_session/rpm_transfer.py`](../wsl_session/rpm_transfer.py) |
| 只在源比目标新时才重新构建 | [`../wsl_session/rpm_stale_build.py`](../wsl_session/rpm_stale_build.py) |
| 等日志里出现某一行 | [`../wsl_session/rpm_wait_logs.py`](../wsl_session/rpm_wait_logs.py) |
| 出错时自动保存全部现场 | [`../wsl_session/rpm_script_meta.py`](../wsl_session/rpm_script_meta.py) |
| 实时看运行过程 / 回放 | 仓库根 [`README.md`](../../README.md) 的「看得见的运行过程」 |
