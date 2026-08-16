"""CORE-11 Workspace 日志组契约（doc/core-lib-requirements.md § CORE-11 日志组）。

规格映射：
    §CORE-11/logs/bootstrap-creates-default    → test_bootstrap_creates_default_log
    §CORE-11/logs/active-pointer-persisted     → test_active_pointer_is_persisted_across_instances
    §CORE-11/logs/run-boundaries-persisted     → test_run_persists_to_active_log_index
    §CORE-11/logs/single-stream-per-log        → test_stream_ndjson_is_single_file_across_runs
    §CORE-11/logs/session-fanout-dir           → test_log_dir_holds_per_session_mirror
    §CORE-11/logs/session-fanout-run-meta      → test_run_boundary_meta_mirrored_to_script_file
    §CORE-11/logs/interrupted-reconstruction   → test_interrupted_run_reconstructed_as_interrupted
    §CORE-11/logs/rename                       → test_rename_log_persists_to_meta
    §CORE-11/logs/rename-rejects-empty         → test_rename_rejects_empty_name
    §CORE-11/logs/rotate-creates-new-active    → test_rotate_creates_new_active_log
    §CORE-11/logs/rotate-refuses-while-running → test_rotate_refuses_while_run_in_progress
    §CORE-11/logs/rotate-refuses-with-queued   → test_rotate_refuses_with_queued_runs
    §CORE-11/logs/pin                          → test_pin_and_unpin_log
    §CORE-11/logs/discard                      → test_discard_removes_log_dir
    §CORE-11/logs/discard-refuses-active       → test_discard_refuses_active_log
    §CORE-11/logs/discard-refuses-pinned       → test_discard_refuses_pinned_log
    §CORE-11/logs/history-view                 → test_list_runs_in_log_after_rotate
    §CORE-11/logs/get-run-across-logs          → test_get_run_finds_historical_run
    §CORE-11/logs/events                       → test_subscribers_receive_log_events
    §CORE-11/logs/reopen-restores-runs         → test_reopen_workspace_restores_active_log_runs
    §CORE-11/logs/new-log-on-start             → test_new_log_on_start_archives_previous_log
                                               → test_new_log_on_start_defaults_off
    §CORE-11/logs/new-log-on-start-reuse       → test_new_log_on_start_reuses_empty_log
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import redpymake as rpm


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


_SIMPLE_SCRIPT = """import redpymake as rpm

def main() -> None:
    with rpm.script('probe'):
        with rpm.local() as sess:
            sess.run('python', '-c', 'print("hi")')
"""


# ------------------------------------------------------------ 引导与恢复


def test_bootstrap_creates_default_log(tmp_path: Path):
    """§CORE-11/logs/bootstrap-creates-default：__enter__ 时若无日志组，创建时间戳命名的默认日志。"""
    with rpm.workspace(tmp_path) as ws:
        active = ws.current_log
        assert active.is_active is True
        assert active.pinned is False
        assert active.run_count == 0
        # 一份日志 = meta.json + 唯一的 stream.ndjson；没有 index、没有 runs/ 子目录
        assert (active.root / "meta.json").is_file()
        assert active.stream_path == active.root / "stream.ndjson"
        assert active.stream_path.exists()
        assert not (active.root / "runs.index.jsonl").exists()
        assert not (active.root / "runs").exists()
        # _active.json 指向它
        pointer = tmp_path / ".redpymake" / "logs" / "_active.json"
        data = json.loads(pointer.read_text(encoding="utf-8"))
        assert data["log_id"] == active.id


def test_active_pointer_is_persisted_across_instances(tmp_path: Path):
    """§CORE-11/logs/active-pointer-persisted：serve 重启同一 root 后仍能拿到上次的活跃日志。"""
    with rpm.workspace(tmp_path) as ws:
        first_id = ws.current_log.id
        ws.rename_log(first_id, "sprint-8")
    # 重开
    with rpm.workspace(tmp_path) as ws2:
        assert ws2.current_log.id == first_id
        assert ws2.current_log.name == "sprint-8"


def test_reopen_workspace_restores_active_log_runs(tmp_path: Path):
    """§CORE-11/logs/reopen-restores-runs：重开 workspace 能读到上一实例已经跑完的 runs。"""
    script = _write(tmp_path, "rpm_x.py", _SIMPLE_SCRIPT)
    first_rid: str
    with rpm.workspace(tmp_path) as ws:
        first_rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(first_rid).status == "succeeded", timeout=15)
        active_id = ws.current_log.id
    with rpm.workspace(tmp_path) as ws2:
        assert ws2.current_log.id == active_id
        assert first_rid in {r.id for r in ws2.runs}
        assert ws2.get_run(first_rid).status == "succeeded"
        # 重建出来的 run 指向该日志唯一的 stream，且能切出自己的记录
        restored = ws2.get_run(first_rid)
        assert restored.ndjson_path == ws2.current_log.stream_path
        assert restored.ndjson_path.exists()
        assert list(ws2.iter_run_records(first_rid)), "重建后应仍能读回该 run 的记录"
        # 旧布局的产物不该出现
        assert not (ws2.current_log.root / "runs.index.jsonl").exists()
        assert not (ws2.current_log.root / "runs").exists()


def test_new_log_on_start_archives_previous_log(tmp_path: Path):
    """§CORE-11/logs/new-log-on-start：serve 语境下每次进入另起一份活跃日志。

    上一份降级为历史但完整保留：仍在 ``list_logs()`` 里，run 记录仍可读。
    """
    script = _write(tmp_path, "rpm_x.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
        first_id = ws.current_log.id

    with rpm.workspace(tmp_path, new_log_on_start=True) as ws2:
        assert ws2.current_log.id != first_id
        assert ws2.current_log.run_count == 0
        assert not ws2.runs, "新日志下不该带上一份日志的 runs"
        # 上一份留作历史，且内容还在
        ids = {log.id for log in ws2.list_logs()}
        assert first_id in ids
        assert [r.id for r in ws2.list_runs_in_log(first_id)] == [rid]


def test_new_log_on_start_reuses_empty_log(tmp_path: Path):
    """§CORE-11/logs/new-log-on-start-reuse：上一份没有 run 时原地复用，不堆空日志。"""
    with rpm.workspace(tmp_path) as ws:
        first_id = ws.current_log.id

    with rpm.workspace(tmp_path, new_log_on_start=True) as ws2:
        assert ws2.current_log.id == first_id
        assert len(ws2.list_logs()) == 1


def test_new_log_on_start_defaults_off(tmp_path: Path):
    """§CORE-11/logs/new-log-on-start：库内默认续用上次活跃日志（REPL/脚本场景）。"""
    script = _write(tmp_path, "rpm_x.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
        first_id = ws.current_log.id

    with rpm.workspace(tmp_path) as ws2:
        assert ws2.current_log.id == first_id
        assert rid in {r.id for r in ws2.runs}


# ------------------------------------------------------------ stream 落盘


def _stream_events(log) -> list[dict]:
    """把一份日志的 stream.ndjson 解析成 dict 列表。"""
    text = log.stream_path.read_text(encoding="utf-8")
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def test_run_persists_to_active_log_index(tmp_path: Path):
    """§CORE-11/logs/run-boundaries-persisted：run 跑完后 stream 里有一对边界元行。

    run 摘要不再单独落盘，全靠 ``workspace.run.begin/end`` 重建。
    """
    script = _write(tmp_path, "rpm_ok.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
        events = _stream_events(ws.current_log)

        begins = [e for e in events if e["event"] == "workspace.run.begin"]
        ends = [e for e in events if e["event"] == "workspace.run.end"]
        assert len(begins) == 1 and len(ends) == 1
        assert begins[0]["run_id"] == rid
        assert begins[0]["log_id"] == ws.current_log.id
        assert begins[0]["script_path"] == str(script.resolve())
        assert ends[0]["run_id"] == rid
        assert ends[0]["status"] == "succeeded"
        assert ends[0]["exception"] is None
        # run_count 从 stream 派生
        assert ws.current_log.run_count == 1


def test_stream_ndjson_is_single_file_across_runs(tmp_path: Path):
    """§CORE-11/logs/single-stream-per-log：一份日志下多个 run 共用一个 stream.ndjson。

    这是本模型的核心约束：不做 per-run / per-script 分片。
    """
    script = _write(tmp_path, "rpm_multi.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rids = []
        for _ in range(3):
            rid = ws.enqueue(script)
            assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
            rids.append(rid)

        log = ws.current_log
        # 目录里只有 meta + 唯一一份流 + 按 session 分文件的旁路镜像目录
        assert sorted(p.name for p in log.root.iterdir()) == [
            "meta.json", "sessions", "stream.ndjson",
        ]
        assert log.run_count == 3

        events = _stream_events(log)
        boundaries = [
            (e["event"], e["run_id"])
            for e in events
            if e["event"] in ("workspace.run.begin", "workspace.run.end")
        ]
        # 三段首尾相接，顺序严格 begin→end 交替
        assert boundaries == [
            ("workspace.run.begin", rids[0]), ("workspace.run.end", rids[0]),
            ("workspace.run.begin", rids[1]), ("workspace.run.end", rids[1]),
            ("workspace.run.begin", rids[2]), ("workspace.run.end", rids[2]),
        ]
        # 每个 run 的切片彼此不重叠
        for rid in rids:
            sliced = list(ws.iter_run_records(rid))
            assert sliced[0]["event"] == "workspace.run.begin"
            assert sliced[0]["run_id"] == rid
            assert sliced[-1]["event"] == "workspace.run.end"
            assert sliced[-1]["run_id"] == rid
            others = {r for r in rids if r != rid}
            assert not (others & {r.get("run_id") for r in sliced})


def test_log_dir_holds_per_session_mirror(tmp_path: Path):
    """§CORE-11/logs/session-fanout-dir：日志组目录下按 session 分文件镜像主流。

    分文件是旁路：stream.ndjson 仍是权威合并流，镜像里每一行都能在主流里找到。
    """
    script = _write(tmp_path, "rpm_fanout.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)

        log = ws.current_log
        sessions_dir = log.root / "sessions"
        assert sessions_dir.is_dir()

        mirrors = sorted(p.name for p in sessions_dir.iterdir())
        assert "__script__.ndjson" in mirrors
        # 脚本里开了一个 local session，除 __script__ 外至少还有一份
        assert len(mirrors) >= 2, mirrors

        stream_lines = {
            line for line in log.stream_path.read_text(encoding="utf-8").splitlines() if line.strip()
        }
        for name in mirrors:
            for line in (sessions_dir / name).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    assert line in stream_lines, f"{name} 里有主流中不存在的行: {line}"


def test_run_boundary_meta_mirrored_to_script_file(tmp_path: Path):
    """§CORE-11/logs/session-fanout-run-meta：workspace.run.begin/end 进 __script__.ndjson。"""
    script = _write(tmp_path, "rpm_meta.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)

        script_file = ws.current_log.root / "sessions" / "__script__.ndjson"
        events = [
            (e.get("event"), e.get("run_id"))
            for e in (json.loads(l) for l in script_file.read_text(encoding="utf-8").splitlines() if l.strip())
        ]
        assert ("workspace.run.begin", rid) in events
        assert ("workspace.run.end", rid) in events
        # 脚本层元行也在，且不夹带任何 session 记录
        assert ("script.begin", None) in events
        assert ("script.end", None) in events


def test_interrupted_run_reconstructed_as_interrupted(tmp_path: Path):
    """§CORE-11/logs/interrupted-reconstruction：孤立 begin 元行重建为 interrupted。

    模拟进程崩溃 / 断电：stream 里留下了 begin 却没来得及写 end。
    """
    script = _write(tmp_path, "rpm_crash.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        done_rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(done_rid).status == "succeeded", timeout=15)
        stream = ws.current_log.stream_path
        log_id = ws.current_log.id

    # 手工往流尾部追一个没有配对 end 的 run 段
    orphan = {
        "event": "workspace.run.begin",
        "run_id": "crash#99",
        "script_path": str(script.resolve()),
        "script_name": "crash",
        "log_id": log_id,
        "started_at": time.time(),
        "timestamp": time.time(),
    }
    with open(stream, "a", encoding="utf-8", newline="\n") as fp:
        fp.write(json.dumps(orphan) + "\n")

    with rpm.workspace(tmp_path) as ws2:
        crashed = ws2.get_run("crash#99")
        assert crashed.status == "interrupted"
        assert crashed.ended_at is None
        assert crashed.script_name == "crash"
        # 正常完成的那个不受影响
        assert ws2.get_run(done_rid).status == "succeeded"
        # 两段都算进 run_count
        assert ws2.current_log.run_count == 2


# ------------------------------------------------------------ rename


def test_rename_log_persists_to_meta(tmp_path: Path):
    """§CORE-11/logs/rename：rename 会把新名字写到 meta.json，重开后仍可读到。"""
    with rpm.workspace(tmp_path) as ws:
        lid = ws.current_log.id
        ws.rename_log(lid, "friendly-name", description="hello")
        assert ws.current_log.name == "friendly-name"
        meta = json.loads((ws.current_log.root / "meta.json").read_text(encoding="utf-8"))
        assert meta["name"] == "friendly-name"
        assert meta["description"] == "hello"


def test_rename_rejects_empty_name(tmp_path: Path):
    """§CORE-11/logs/rename-rejects-empty：空字符串名字应被拒绝，避免"匿名日志"。"""
    with rpm.workspace(tmp_path) as ws:
        lid = ws.current_log.id
        with pytest.raises(ValueError):
            ws.rename_log(lid, "")
        with pytest.raises(ValueError):
            ws.rename_log(lid, "   ")


# ------------------------------------------------------------ rotate


def test_rotate_creates_new_active_log(tmp_path: Path):
    """§CORE-11/logs/rotate-creates-new-active：rotate 归档旧的、新的成为活跃，_active.json 更新。"""
    with rpm.workspace(tmp_path) as ws:
        old_id = ws.current_log.id
        new_log = ws.rotate_log(name="phase-2")
        assert new_log.id != old_id
        assert new_log.is_active is True
        assert new_log.name == "phase-2"
        # 旧的仍在但 is_active=False
        old = ws.get_log(old_id)
        assert old.is_active is False
        # pointer 也变了
        pointer = tmp_path / ".redpymake" / "logs" / "_active.json"
        assert json.loads(pointer.read_text(encoding="utf-8"))["log_id"] == new_log.id
        # 内存 runs 视图应该空
        assert list(ws.runs) == []


def test_rotate_refuses_while_run_in_progress(tmp_path: Path):
    """§CORE-11/logs/rotate-refuses-while-running：有 run 在跑时 rotate 抛 RuntimeError。"""
    # 弄一个能"卡住"的脚本：sleep 一段时间
    script = _write(
        tmp_path,
        "rpm_slow.py",
        """import redpymake as rpm, time

def main() -> None:
    with rpm.script('slow'):
        with rpm.local() as sess:
            sess.run('python', '-c', 'import time; time.sleep(2)')
""",
    )
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "running", timeout=5)
        with pytest.raises(RuntimeError, match="in progress"):
            ws.rotate_log()
        # 等它跑完再收尾，避免污染
        assert _wait_until(
            lambda: ws.get_run(rid).status in {"succeeded", "failed"}, timeout=15
        )


def test_rotate_refuses_with_queued_runs(tmp_path: Path):
    """§CORE-11/logs/rotate-refuses-with-queued：队列里还有排队的 run 时 rotate 也拒绝。"""
    script = _write(tmp_path, "rpm_a.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        ws.pause_queue()
        rid = ws.enqueue(script)
        # 此时 run 还在 queued
        assert ws.get_run(rid).status == "queued"
        with pytest.raises(RuntimeError, match="queued"):
            ws.rotate_log()
        # 让它跑完再收尾
        ws.resume_queue()
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)


# ------------------------------------------------------------ pin & discard


def test_pin_and_unpin_log(tmp_path: Path):
    """§CORE-11/logs/pin：pin/unpin 双向切换写盘。"""
    with rpm.workspace(tmp_path) as ws:
        lid = ws.current_log.id
        assert ws.current_log.pinned is False
        ws.pin_log(lid, True)
        assert ws.get_log(lid).pinned is True
        meta = json.loads((ws.get_log(lid).root / "meta.json").read_text(encoding="utf-8"))
        assert meta["pinned"] is True
        ws.pin_log(lid, False)
        assert ws.get_log(lid).pinned is False


def test_discard_refuses_active_log(tmp_path: Path):
    """§CORE-11/logs/discard-refuses-active：活跃日志不允许 discard。"""
    with rpm.workspace(tmp_path) as ws:
        with pytest.raises(RuntimeError, match="active"):
            ws.discard_log(ws.current_log.id)


def test_discard_refuses_pinned_log(tmp_path: Path):
    """§CORE-11/logs/discard-refuses-pinned：pinned 的历史日志不允许 discard。"""
    with rpm.workspace(tmp_path) as ws:
        old_id = ws.current_log.id
        ws.pin_log(old_id, True)
        ws.rotate_log()
        assert ws.get_log(old_id).pinned is True
        with pytest.raises(RuntimeError, match="pinned"):
            ws.discard_log(old_id)


def test_discard_removes_log_dir(tmp_path: Path):
    """§CORE-11/logs/discard：非活跃非 pinned 日志能被硬删（目录整个消失）。"""
    with rpm.workspace(tmp_path) as ws:
        old_id = ws.current_log.id
        old_root = ws.current_log.root
        ws.rotate_log()
        assert old_root.exists()
        ws.discard_log(old_id)
        assert not old_root.exists()
        with pytest.raises(KeyError):
            ws.get_log(old_id)


# ------------------------------------------------------------ 历史视图


def test_list_runs_in_log_after_rotate(tmp_path: Path):
    """§CORE-11/logs/history-view：rotate 后旧日志的 runs 仍可通过 list_runs_in_log 读到。"""
    script = _write(tmp_path, "rpm_h.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        old_id = ws.current_log.id
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
        ws.rotate_log()
        # 活跃日志的 runs 空了
        assert ws.runs == []
        # 旧日志仍能列出该 run
        old_runs = ws.list_runs_in_log(old_id)
        assert [r.id for r in old_runs] == [rid]
        assert old_runs[0].status == "succeeded"


def test_get_run_finds_historical_run(tmp_path: Path):
    """§CORE-11/logs/get-run-across-logs：get_run 对已归档到历史日志的 run 也能命中。"""
    script = _write(tmp_path, "rpm_gh.py", _SIMPLE_SCRIPT)
    with rpm.workspace(tmp_path) as ws:
        rid = ws.enqueue(script)
        assert _wait_until(lambda: ws.get_run(rid).status == "succeeded", timeout=15)
        ws.rotate_log()
        # 跨日志查找应成功
        got = ws.get_run(rid)
        assert got.id == rid
        assert got.status == "succeeded"


# ------------------------------------------------------------ 事件


def test_subscribers_receive_log_events(tmp_path: Path):
    """§CORE-11/logs/events：rename/rotate/pin/discard 会广播对应事件。"""
    with rpm.workspace(tmp_path) as ws:
        events: list[dict] = []
        ws.subscribe(events.append)
        lid = ws.current_log.id
        ws.rename_log(lid, "new-name")
        new_log = ws.rotate_log()
        ws.pin_log(lid, True)
        ws.pin_log(lid, False)
        ws.discard_log(lid)
    types = [e["type"] for e in events]
    assert "log.renamed" in types
    assert "log.rotated" in types
    assert "log.pinned" in types
    assert "log.discarded" in types
    # rotate 事件携带 previous_log_id
    rotated = next(e for e in events if e["type"] == "log.rotated")
    assert rotated["previous_log_id"] == lid
    assert rotated["log_id"] == new_log.id
