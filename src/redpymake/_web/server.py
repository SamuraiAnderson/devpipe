"""FastAPI Web UI (CORE-11)。

导出 :func:`build_app` — 传入一个已 ``__enter__`` 的 ``Workspace`` 实例，返回
FastAPI ``app``。所有路由**只调 workspace 的方法**，不引入独立业务状态。

WebSocket ``/ws`` 广播两类事件：

- 状态转移：``run.enqueued`` / ``run.started`` / ``run.finished``；
- 增量 record：``{"type":"run.record","run_id":..,"record":{...}}``——由
  ``Workspace._make_script_record_hook`` 挂在活跃 ``ScriptRun`` 上产生，让浏览器
  端拿到实时时间轴 & 回放数据。

依赖 ``fastapi`` / ``jinja2`` / ``websockets``（``redpymake[web]`` extra）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:  # pragma: no cover
    from .._workspace import Workspace


_diag_logger = logging.getLogger("redpymake")
_STATIC_DIR = Path(__file__).parent / "static"


def build_app(ws: "Workspace") -> FastAPI:
    """构造 FastAPI 应用，把它绑到给定的 ``Workspace``。

    调用方负责管理 ``ws`` 生命周期（``with ws: ...``）。
    """
    app = FastAPI(title="RedPyMake", docs_url=None, redoc_url=None)
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_render_index_html())

    @app.get("/api/scripts")
    async def api_scripts() -> Any:
        return [card.to_dict() for card in ws.discover()]

    @app.get("/api/sessions")
    async def api_sessions() -> Any:
        result: list[dict] = []
        for key, sess in ws.sessions().items():
            result.append(
                {
                    "key": key,
                    "session_id": sess.session_id,
                    "kind": sess.kind,
                    "label": sess.label,
                    "closed": sess.closed,
                }
            )
        return result

    @app.get("/api/runs")
    async def api_runs() -> Any:
        return [run.to_dict() for run in ws.runs]

    @app.get("/api/runs/current")
    async def api_current_run() -> Any:
        run = ws.current_run
        return run.to_dict() if run is not None else None

    @app.post("/api/runs")
    async def api_start_run(payload: dict) -> Any:
        path = payload.get("path")
        if not path:
            raise HTTPException(400, "missing 'path' in body")
        try:
            rid = ws.enqueue(path)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"run_id": rid}, status_code=201)

    @app.post("/api/runs/stop")
    async def api_stop_current() -> Any:
        """不带 rid 的旧入口：停当前 run（谁在跑停谁）。"""
        stopped = ws.stop_current()
        return {"ok": True, "stopped": stopped}

    @app.get("/api/runs/{run_id}/records")
    async def api_run_records(run_id: str) -> Any:
        try:
            run = ws.get_run(run_id)
        except KeyError:
            raise HTTPException(404, f"run not found: {run_id}")
        return {
            "run": run.to_dict(),
            "records": list(ws.iter_run_records(run_id)),
        }

    @app.post("/api/runs/{run_id}/stop")
    async def api_stop_run(run_id: str) -> Any:
        """协作式停止指定 run；仅当它就是当前正在跑的那个。

        停止是"尽力而为"：不强杀已经跑起来的子进程，脚本在下一条命令边界退出。
        前端据此把按钮置灰显示 ``stopping…``，直到 ``run.finished`` 事件到达。
        """
        try:
            ws.get_run(run_id)
        except KeyError:
            raise HTTPException(404, f"run not found: {run_id}")
        current = ws.current_run
        if current is None or current.id != run_id:
            raise HTTPException(409, f"run is not currently running: {run_id}")
        stopped = ws.stop_current()
        return {"ok": True, "stopped": stopped, "run_id": run_id}

    @app.post("/api/runs/{run_id}/rerun")
    async def api_rerun(run_id: str) -> Any:
        """用同一脚本路径重新入队；原 run 记录保持不动。"""
        try:
            new_rid = ws.rerun(run_id)
        except KeyError:
            raise HTTPException(404, f"run not found: {run_id}")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"run_id": new_rid, "source_run_id": run_id}, status_code=201)

    @app.delete("/api/runs/{run_id}")
    async def api_cancel_run(run_id: str) -> Any:
        """把仍在排队的 run 剔出队列；已经在跑的请改用 stop。"""
        try:
            ws.get_run(run_id)
        except KeyError:
            raise HTTPException(404, f"run not found: {run_id}")
        cancelled = ws.cancel_run(run_id)
        if not cancelled:
            raise HTTPException(409, f"run is not queued: {run_id}")
        return {"ok": True, "run_id": run_id}

    # -------------------------------------------------- 日志组 (§CORE-11)

    @app.get("/api/logs")
    async def api_logs() -> Any:
        return [log.to_dict() for log in ws.list_logs()]

    @app.get("/api/logs/current")
    async def api_current_log() -> Any:
        try:
            return ws.current_log.to_dict()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/logs/{log_id}")
    async def api_get_log(log_id: str) -> Any:
        try:
            return ws.get_log(log_id).to_dict()
        except KeyError:
            raise HTTPException(404, f"log not found: {log_id}")

    @app.get("/api/logs/{log_id}/runs")
    async def api_log_runs(log_id: str) -> Any:
        try:
            runs = ws.list_runs_in_log(log_id)
        except KeyError:
            raise HTTPException(404, f"log not found: {log_id}")
        return [r.to_dict() for r in runs]

    @app.get("/api/logs/{log_id}/runs/{run_id}/records")
    async def api_log_run_records(log_id: str, run_id: str) -> Any:
        """历史日志里某个 run 的记录区间。

        前端浏览非活跃日志时走这条；活跃日志的 run 直接从前端 liveBuffer 过滤，
        不必回服务端。
        """
        try:
            runs = ws.list_runs_in_log(log_id)
        except KeyError:
            raise HTTPException(404, f"log not found: {log_id}")
        match = next((r for r in runs if r.id == run_id), None)
        if match is None:
            raise HTTPException(404, f"run not found in log {log_id}: {run_id}")
        return {
            "run": match.to_dict(),
            "records": list(ws.iter_run_records(run_id)),
        }

    @app.patch("/api/logs/{log_id}")
    async def api_rename_log(log_id: str, payload: dict) -> Any:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "missing 'name' in body")
        try:
            log = ws.rename_log(
                log_id, name, description=payload.get("description")
            )
        except KeyError:
            raise HTTPException(404, f"log not found: {log_id}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return log.to_dict()

    @app.post("/api/logs/rotate")
    async def api_rotate_log(payload: dict | None = None) -> Any:
        payload = payload or {}
        try:
            log = ws.rotate_log(name=payload.get("name"))
        except RuntimeError as exc:
            # 409 Conflict：有 run 在跑或排队；前端展示提示 + 让用户选等 / 停
            raise HTTPException(409, str(exc))
        return JSONResponse(log.to_dict(), status_code=201)

    @app.post("/api/logs/{log_id}/pin")
    async def api_pin_log(log_id: str, payload: dict | None = None) -> Any:
        payload = payload or {}
        pinned = payload.get("pinned", True)
        try:
            log = ws.pin_log(log_id, pinned=bool(pinned))
        except KeyError:
            raise HTTPException(404, f"log not found: {log_id}")
        return log.to_dict()

    @app.delete("/api/logs/{log_id}")
    async def api_discard_log(log_id: str) -> Any:
        try:
            ws.discard_log(log_id)
        except KeyError:
            raise HTTPException(404, f"log not found: {log_id}")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    # -------------------------------------------------- 手动命令执行 (Web UI 终端)

    @app.post("/api/commands")
    async def api_execute_command(payload: dict) -> Any:
        """执行手动命令，返回 command_id 用于跟踪。

        请求体：{"session_id": "...", "command": "...", "shell": false, "cwd": null, "timeout": 30}
        返回：{"command_id": "...", "session_id": "...", "status": "running", "started_at": ...}

        输出通过 WebSocket 的 cmd.output/cmd.finished/cmd.error 事件流式推送。
        """
        session_id = payload.get("session_id")
        command = payload.get("command")
        if not session_id or not command:
            raise HTTPException(400, "missing 'session_id' or 'command' in body")

        shell = payload.get("shell", False)
        cwd = payload.get("cwd")
        timeout = payload.get("timeout", 30.0)

        try:
            cmd_id = ws.command_executor.execute(
                session_id=session_id,
                command=command,
                shell=bool(shell),
                cwd=cwd,
                timeout=float(timeout),
            )
            return {
                "command_id": cmd_id,
                "session_id": session_id,
                "status": "running",
                "started_at": __import__("time").time(),
            }
        except Exception as exc:
            _diag_logger.exception("api_execute_command failed")
            raise HTTPException(500, f"command execution failed: {exc}")

    @app.get("/api/commands/history")
    async def api_get_command_history(
        session_id: str | None = None, limit: int = 100
    ) -> Any:
        """获取命令历史。

        参数：
        - session_id: 可选，过滤特定 session
        - limit: 最大返回条数
        """
        try:
            return ws.command_executor.get_history(session_id=session_id, limit=limit)
        except Exception as exc:
            _diag_logger.exception("api_get_command_history failed")
            raise HTTPException(500, f"failed to get history: {exc}")

    @app.delete("/api/commands/history")
    async def api_clear_command_history(session_id: str | None = None) -> Any:
        """清空命令历史。

        参数：
        - session_id: 可选，清空特定 session；为空则清空全部
        """
        try:
            ok = ws.command_executor.clear_history(session_id=session_id)
            return {"ok": ok}
        except Exception as exc:
            _diag_logger.exception("api_clear_command_history failed")
            raise HTTPException(500, f"failed to clear history: {exc}")

    @app.websocket("/ws")
    async def ws_endpoint(sock: WebSocket) -> None:
        await sock.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        loop = asyncio.get_event_loop()

        def _forward(event: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception:  # pragma: no cover
                pass

        unsub = ws.subscribe(_forward)
        try:
            while True:
                event = await queue.get()
                await sock.send_json(event)
        except WebSocketDisconnect:  # pragma: no cover - 客户端断开是正常路径
            pass
        except Exception:  # pragma: no cover
            _diag_logger.exception("websocket loop error")
        finally:
            try:
                unsub()
            except Exception:  # pragma: no cover
                pass

    return app


# ------------------------------------------------------------ 首屏 HTML


def _render_index_html() -> str:
    """服务端渲染主页；纯 vanilla JS，没有构建链。"""
    return _INDEX_HTML


_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>RedPyMake Workspace</title>
<link rel="stylesheet" href="/static/styles.css"/>
</head>
<body class="app-shell">
<header>
  <div class="header-left">
    <h1>RedPyMake Workspace</h1>
    <p class="hint">发现 / 会话池 / 运行历史 / 实时日志流 &amp; 回放</p>
  </div>
  <div class="header-right" id="log-switcher">
    <div class="log-current">
      <span class="log-eyebrow">Log</span>
      <button class="log-current-btn" id="log-current-btn" title="切换日志">
        <span id="log-current-name">(loading)</span>
        <span class="log-current-count hint" id="log-current-count"></span>
        <span class="chev">&#9662;</span>
      </button>
      <div class="log-dropdown" id="log-dropdown" hidden></div>
    </div>
    <button class="tb-btn" id="log-rename-btn" title="重命名当前日志">&#9998;</button>
    <button class="tb-btn" id="log-pin-btn" title="pin / unpin 当前日志">&#9733;</button>
    <button class="tb-btn" id="log-rotate-btn" title="归档当前日志并新建（清空）">&#8635; New log</button>
  </div>
</header>
<div class="layout">
  <aside class="sidebar">
    <h3 class="sb-head">
      <span>Sessions</span>
      <button class="sb-head-btn" id="split-toggle" title="按 session 分栏查看主区日志">&#9707; Split</button>
    </h3>
    <ul id="sess-list"><li class="hint">(none)</li></ul>
    <h3>Scripts</h3>
    <ul id="scripts-list" class="sb-list"><li class="hint">(loading)</li></ul>
    <h3>Runs</h3>
    <ul id="runs-list" class="sb-list"><li class="hint">(none)</li></ul>
  </aside>
  <main class="main-panel">
    <div class="main-toolbar" id="main-toolbar" hidden>
      <button class="back-to-live-btn" id="back-to-live" title="回到实时日志流">&#8677; Back to Live</button>
      <span class="hint" id="main-context"></span>
    </div>
    <div id="live-root" class="panel-view"></div>
    <div id="timeline-root" class="panel-view" hidden></div>
  </main>
</div>
<script src="/static/vendor/split.min.js"></script>
<script src="/static/timeline.js"></script>
<script>
(function () {
  // liveBuffer 挂钩**活跃日志**：跨 run 持续累积，切去看历史 run 不打断，
  // 切回 Live 依然能看到完整尾部。只有换日志组时才重置。
  var LIVE_BUFFER_MAX = 8000;

  var SPLIT_STORAGE_KEY = "rpm.split";

  // ================================================== store（事务化）
  //
  // 用户动作 / 服务端事件 → dispatch(action) → reducer 纯函数算出新 state
  // → 订阅者把 state 投影到 DOM。
  //
  // 硬规矩：reducer 里不许 fetch、不许碰 localStorage、不许碰 DOM。副作用一律
  // 留在 dispatch 的调用方或订阅者里。这样 action 序列就是一份可回放的操作史，
  // 也是将来把 UI 操作并进 timeline replay 的接口。
  //
  // 组件实例与高频 buffer 不进 store（见 refs）——它们只在当前渲染帧有意义，
  // 塞进来只会让每条日志都触发一次全量状态复制。

  function assign(base, patch) {
    var out = {}, k;
    for (k in base) if (Object.prototype.hasOwnProperty.call(base, k)) out[k] = base[k];
    for (k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) out[k] = patch[k];
    return out;
  }

  function createStore(reducer, initial) {
    var current = initial;
    var subs = [];
    return {
      getState: function () { return current; },
      dispatch: function (action) {
        var next = reducer(current, action);
        if (next === current) return current;   // 无变化的 action 不惊动订阅者
        var prev = current;
        current = next;
        if (window.__rpmTraceActions) console.debug("[act]", action.type, action);
        for (var i = 0; i < subs.length; i++) subs[i](current, prev, action);
        return current;
      },
      subscribe: function (fn) {
        subs.push(fn);
        return function () {
          var i = subs.indexOf(fn);
          if (i >= 0) subs.splice(i, 1);
        };
      },
    };
  }

  var initialState = {
    view: "live",            // "live" | "run"
    viewedRunId: null,       // view === "run" 时主区在看哪个 run
    currentRunId: null,      // 服务端当前正在跑的 run（只影响 sidebar，不抢主区）
    scripts: [],
    scriptsByPath: {},
    runs: [],                // 当前浏览范围内的 run 列表（服务端顺序）
    runsById: {},
    logs: [],
    currentLogId: null,
    viewedLogId: null,       // sidebar Runs 展示范围；默认 = currentLogId
    stopping: {},            // rid -> true：已按下 stop，等 run.finished 复位
    // 会话唯一事实来源：池里的 + 主区数据里出现过的历史会话都记在这
    // sid -> { label, live }；live=false 表示不在池里（历史 run 的会话）
    sessions: {},
    // 分栏：order 是列顺序的唯一权威，widths 存 Split.js 拖出来的百分比
    split: { enabled: false, order: [], widths: {} },
  };

  // 非事务态：组件实例与高频 buffer，不参与 reducer
  var refs = {
    liveTail: null,          // LiveTail 实例，常驻不销毁
    timeline: null,          // Run detail 的 Timeline 实例
    liveBuffer: [],
    liveBufferLogId: null,   // liveBuffer 属于哪个 log
    paneTimer: null,
    persistTimer: null,
  };

  // 只有 kind:label#N 形态的才是真会话；script:* 是脚本自身 user_log 的合成 id
  function isValidSessionId(sid) {
    return typeof sid === "string" && !isScriptSession(sid)
      && /^[^:]+:[^#]+#\d+$/.test(sid);
  }

  // 轮询回来的数据大多没变；这几个浅比较让空转的 poll 不触发任何重绘
  function sameSessions(a, b) {
    var ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    for (var i = 0; i < ka.length; i++) {
      var k = ka[i];
      if (!b[k] || b[k].label !== a[k].label || b[k].live !== a[k].live) return false;
    }
    return true;
  }

  function sameList(a, b, fields) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) {
      for (var j = 0; j < fields.length; j++) {
        if (a[i][fields[j]] !== b[i][fields[j]]) return false;
      }
    }
    return true;
  }

  function reducer(state, action) {
    switch (action.type) {

      case "SPLIT_PREF_LOADED":
        return assign(state, {
          split: { enabled: action.enabled, order: action.order, widths: action.widths },
        });

      case "SPLIT_TOGGLE": {
        var enabled = !state.split.enabled;
        var order = state.split.order;
        // 首次开启：把见过的会话全部铺开；sessions 的键天然唯一，无需去重
        if (enabled && !order.length) order = Object.keys(state.sessions);
        return assign(state, {
          split: assign(state.split, { enabled: enabled, order: order }),
        });
      }

      case "PANE_TOGGLE": {
        var at = state.split.order.indexOf(action.sid);
        var nextOrder;
        if (at >= 0) {
          nextOrder = state.split.order.slice();
          nextOrder.splice(at, 1);
        } else {
          if (!isValidSessionId(action.sid)) return state;
          nextOrder = state.split.order.concat([action.sid]);
        }
        return assign(state, { split: assign(state.split, { order: nextOrder }) });
      }

      case "PANE_RESIZED":
        return assign(state, { split: assign(state.split, { widths: action.widths }) });

      case "SESSION_APPEARED": {
        if (!isValidSessionId(action.sid)) return state;
        var prev = state.sessions[action.sid];
        var label = action.label || (prev && prev.label) || action.sid;
        var live = action.live === undefined ? (prev ? prev.live : false) : !!action.live;
        if (prev && prev.label === label && prev.live === live) return state;
        var sessions = assign(state.sessions, {});
        sessions[action.sid] = { label: label, live: live };
        var split = state.split;
        // 分栏开着时新冒出来的会话直接给一列，免得它悄悄落进 Script 兜底列
        if (!prev && split.enabled && split.order.indexOf(action.sid) < 0) {
          split = assign(split, { order: split.order.concat([action.sid]) });
        }
        return assign(state, { sessions: sessions, split: split });
      }

      case "SESSIONS_SYNCED": {
        // 池外（历史 run）的会话保留，只把 live 标记降下来
        var synced = {};
        Object.keys(state.sessions).forEach(function (sid) {
          synced[sid] = assign(state.sessions[sid], { live: false });
        });
        var split2 = state.split;
        (action.list || []).forEach(function (s) {
          if (!isValidSessionId(s.session_id)) return;
          var known = !!state.sessions[s.session_id];
          synced[s.session_id] = { label: s.kind + " / " + s.label, live: !s.closed };
          if (!known && split2.enabled && split2.order.indexOf(s.session_id) < 0) {
            split2 = assign(split2, { order: split2.order.concat([s.session_id]) });
          }
        });
        if (split2 === state.split && sameSessions(synced, state.sessions)) return state;
        return assign(state, { sessions: synced, split: split2 });
      }

      case "SCRIPTS_LOADED": {
        var scripts = action.scripts || [];
        if (sameList(scripts, state.scripts, ["path", "error", "script_name"])) return state;
        var byPath = {};
        scripts.forEach(function (c) { byPath[c.path] = c; });
        return assign(state, { scripts: scripts, scriptsByPath: byPath });
      }

      case "RUNS_LOADED": {
        var runs = action.runs || [];
        if (sameList(runs, state.runs, ["id", "status", "ended_at"])) return state;
        var byId = assign(state.runsById, {});
        var running = null;
        runs.forEach(function (r) {
          byId[r.id] = r;
          if (r.status === "running") running = r.id;
        });
        return assign(state, { runs: runs, runsById: byId, currentRunId: running });
      }

      case "RUN_DETAIL_LOADED": {
        var withRun = assign(state.runsById, {});
        withRun[action.run.id] = action.run;
        return assign(state, { runsById: withRun });
      }

      case "RUN_STOP_REQUESTED": {
        if (state.stopping[action.rid]) return state;
        var marked = assign(state.stopping, {});
        marked[action.rid] = true;
        return assign(state, { stopping: marked });
      }

      // 乐观置灰的回滚点：请求被拒 / run 真的结束了，都在这里复位
      case "RUN_STOP_FAILED":
      case "RUN_FINISHED": {
        if (!state.stopping[action.rid]) return state;
        var cleared = assign(state.stopping, {});
        delete cleared[action.rid];
        return assign(state, { stopping: cleared });
      }

      case "VIEW_CHANGED": {
        var vr = action.viewedRunId || null;
        if (state.view === action.view && state.viewedRunId === vr) return state;
        return assign(state, { view: action.view, viewedRunId: vr });
      }

      case "LOGS_LOADED": {
        var logs = action.logs || [];
        var same = sameList(logs, state.logs,
          ["id", "name", "is_active", "pinned", "run_count"]);
        var active = logs.filter(function (l) { return l.is_active; })[0] || null;
        if (!active) return same ? state : assign(state, { logs: logs });
        var switched = state.currentLogId && state.currentLogId !== active.id;
        var viewed = (!state.viewedLogId || switched) ? active.id : state.viewedLogId;
        if (same && state.currentLogId === active.id && state.viewedLogId === viewed) return state;
        return assign(state, { logs: logs, currentLogId: active.id, viewedLogId: viewed });
      }

      case "LOG_SELECTED":
        if (state.viewedLogId === action.logId) return state;
        return assign(state, { viewedLogId: action.logId });

      case "LOG_ROTATED":
        return assign(state, { currentLogId: action.logId, viewedLogId: action.logId });

      case "LOG_DISCARDED":
        if (state.viewedLogId !== action.logId) return state;
        return assign(state, { viewedLogId: state.currentLogId });

      default:
        return state;
    }
  }

  var store = createStore(reducer, initialState);
  var dispatch = store.dispatch;

  // 读侧别名：所有 state.xxx 读取保持原样，只有写入换成 dispatch。
  // 必须是第一个订阅者，后面的渲染订阅者才能读到最新值。
  var state = initialState;
  store.subscribe(function (next) { state = next; });

  function loadSplitPref() {
    var saved;
    try {
      var raw = window.localStorage.getItem(SPLIT_STORAGE_KEY);
      if (!raw) return;
      saved = JSON.parse(raw);
    } catch (e) { return; /* localStorage 不可用（隐私模式等）→ 用默认值 */ }
    if (!saved || typeof saved !== "object") return;
    // sessionIds 是旧 schema，读到就迁到 order 并回写一次
    var legacy = !saved.order && saved.sessionIds;
    var ids = saved.order || saved.sessionIds || [];
    var order = [];
    for (var i = 0; i < ids.length; i++) {
      if (isValidSessionId(ids[i]) && order.indexOf(ids[i]) < 0) order.push(ids[i]);
    }
    dispatch({
      type: "SPLIT_PREF_LOADED",
      enabled: !!saved.enabled,
      order: order,
      widths: (saved.widths && typeof saved.widths === "object") ? saved.widths : {},
    });
    if (legacy || order.length !== ids.length) persistSplit(true);
  }

  // 订阅者副作用：state.split 一变就落盘，攒一拍避免拖动时高频写
  function persistSplit(immediate) {
    if (refs.persistTimer) {
      clearTimeout(refs.persistTimer);
      refs.persistTimer = null;
    }
    var write = function () {
      refs.persistTimer = null;
      try {
        window.localStorage.setItem(SPLIT_STORAGE_KEY, JSON.stringify(state.split));
      } catch (e) { /* ignore */ }
    };
    if (immediate) write();
    else refs.persistTimer = setTimeout(write, 250);
  }

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === "className") n.className = attrs[k];
        else if (k === "onclick") n.onclick = attrs[k];
        else n.setAttribute(k, attrs[k]);
      }
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      if (typeof c === "string") n.appendChild(document.createTextNode(c));
      else n.appendChild(c);
    });
    return n;
  }

  function statusIcon(status) {
    return ({
      queued: "\u23F8", running: "\u25CF", succeeded: "\u2713",
      failed: "\u2717", cancelled: "\u25CB", interrupted: "\u26A1",
    }[status] || "?");
  }

  function isTerminal(status) {
    return status === "succeeded" || status === "failed"
      || status === "cancelled" || status === "interrupted";
  }

  function activeLog() {
    return state.logs.filter(function (l) { return l.is_active; })[0] || null;
  }

  // -------------------------------------------------- 分栏

  // 脚本自身的 user_log 走合成 id script:<name>#N —— 它不是真会话，固定进 Script 列
  function isScriptSession(sid) {
    return typeof sid === "string" && sid.indexOf("script:") === 0;
  }

  // 记下见过的 session：池里的、以及主区数据里出现过的（历史 run 的会话不在池里）。
  // 每条日志都会走这里，reducer 认得出重复就原样返回，dispatch 会短路掉。
  function noteSession(sid, label) {
    dispatch({ type: "SESSION_APPEARED", sid: sid, label: label });
  }

  // order 是唯一权威；只需过滤掉还没见过的 sid，不需要任何去重补丁
  function paneDefs() {
    if (!state.split.enabled) return [];
    return state.split.order
      .filter(function (sid) { return !!state.sessions[sid]; })
      .map(function (sid) { return { id: sid, label: state.sessions[sid].label }; });
  }

  function applyPanes() {
    var defs = paneDefs();
    // setPanes 内部按 id diff，列没变时是廉价空转，可以放心多调
    if (refs.liveTail) refs.liveTail.setPanes(defs, refs.liveBuffer);
    if (refs.timeline) refs.timeline.setPanes(defs);
  }

  // 一次 run 里会连续冒出好几个新 session，攒一拍再重建列；
  // 用户自己点的（toggle）要立刻响应，不能等这 150ms
  function schedulePanes(immediate) {
    if (immediate) {
      if (refs.paneTimer) { clearTimeout(refs.paneTimer); refs.paneTimer = null; }
      applyPanes();
      return;
    }
    if (refs.paneTimer) return;
    refs.paneTimer = setTimeout(function () {
      refs.paneTimer = null;
      applyPanes();
    }, 150);
  }

  // PaneSet（timeline.js）与宿主之间的宽度桥：Live 和 Run detail 两个 PaneSet
  // 共用 state.split.widths，拖任意一个另一个下次重建时就跟上。
  window.__rpmLoadWidths = function () {
    return state.split.widths || {};
  };
  window.__rpmSaveWidths = function (widths) {
    dispatch({ type: "PANE_RESIZED", widths: widths });
  };

  function toggleSplit() {
    dispatch({ type: "SPLIT_TOGGLE" });
  }

  function toggleSessionPane(sid) {
    dispatch({ type: "PANE_TOGGLE", sid: sid });
  }

  function syncSplitButton() {
    document.getElementById("split-toggle")
      .classList.toggle("active", state.split.enabled);
  }

  // -------------------------------------------------- 主区：Live tail / Run detail

  function ensureLiveTail() {
    if (!refs.liveTail) {
      refs.liveTail = window.__RedPyMakeTimeline.mountLive(document.getElementById("live-root"));
      var log = activeLog();
      if (log) refs.liveTail.setLog(log.name);
      if (state.split.enabled) refs.liveTail.setPanes(paneDefs(), refs.liveBuffer);
    }
    return refs.liveTail;
  }

  function pushLive(rec, rid) {
    if (!rec) return;
    // 打标便于 Run detail 直接从 buffer 里切片，省一次服务端往返
    if (rid) rec.__rid = rid;
    noteSession(rec.session_id);
    refs.liveBuffer.push(rec);
    if (refs.liveBuffer.length > LIVE_BUFFER_MAX) refs.liveBuffer.shift();
    ensureLiveTail().append(rec);
  }

  function resetLiveBuffer(logId) {
    refs.liveBuffer = [];
    refs.liveBufferLogId = logId || null;
    var tail = ensureLiveTail();
    tail.clear();
    var log = activeLog();
    if (log) tail.setLog(log.name);
  }

  function showLive() {
    refs.timeline = null;
    document.getElementById("timeline-root").hidden = true;
    document.getElementById("live-root").hidden = false;
    document.getElementById("main-toolbar").hidden = true;
    dispatch({ type: "VIEW_CHANGED", view: "live", viewedRunId: null });
    ensureLiveTail().scrollToEnd();
    refreshRuns();
  }

  function showRunDetail(rid) {
    document.getElementById("live-root").hidden = true;
    document.getElementById("timeline-root").hidden = false;
    document.getElementById("main-toolbar").hidden = false;
    document.getElementById("main-context").textContent = "Run detail · " + rid;
    dispatch({ type: "VIEW_CHANGED", view: "run", viewedRunId: rid });

    var known = state.runsById[rid];
    var buffered = refs.liveBuffer.filter(function (r) { return r.__rid === rid; });
    if (buffered.length && known) {
      mountRunTimeline(known, buffered);
      refreshRuns();
      return;
    }
    // buffer 里没有（历史 run / 刷新过页面）→ 回服务端要该 run 的流片段
    var url = (state.viewedLogId && state.viewedLogId !== state.currentLogId)
      ? "/api/logs/" + encodeURIComponent(state.viewedLogId) + "/runs/" + encodeURIComponent(rid) + "/records"
      : "/api/runs/" + encodeURIComponent(rid) + "/records";
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (!data || !data.run) return;
      dispatch({ type: "RUN_DETAIL_LOADED", run: data.run });
      mountRunTimeline(data.run, data.records || []);
      refreshRuns();
    });
  }

  function mountRunTimeline(run, records) {
    // 历史 run 的会话早已不在池里，只能从记录里认出来，否则没法勾选分栏
    (records || []).forEach(function (r) { noteSession(r.session_id); });
    refs.timeline = window.__RedPyMakeTimeline.mount(document.getElementById("timeline-root"), {
      meta: {
        name: (run.script_name || run.id) + "  ·  " + run.id,
        started_at: run.started_at,
        ended_at: run.ended_at,
        exception: run.exception ? { type: "Exception", message: run.exception } : null,
        status: run.status,
      },
      records: records,
    });
    refs.timeline.setPanes(paneDefs());
    refreshSessions();
  }

  // -------------------------------------------------- Sidebar

  function refreshScripts() {
    fetch("/api/scripts").then(function (r) { return r.json(); }).then(function (data) {
      dispatch({ type: "SCRIPTS_LOADED", scripts: data || [] });
    });
  }

  function renderScriptList() {
      var list = document.getElementById("scripts-list");
      list.innerHTML = "";
      var data = state.scripts;
      if (!data.length) {
        list.appendChild(el("li", { className: "hint" }, ["(none)"]));
        return;
      }
      data.forEach(function (c) {
        var label = c.script_name || c.path.split(/[\\/]/).pop();
        // 没有 rpm.script(...) 块的脚本能跑，但不会有聚合日志——标个警示
        var warn = c.has_script_block ? "" : " \u26A0";
        var tip = c.path + (c.error ? "\n\nparse error: " + c.error : "");
        // 点条目本身不做任何事：运行必须点右侧 ▶，避免误触
        var li = el("li", { className: "sb-item", title: tip }, [
          el("span", { className: "sb-label" }, [label + warn]),
        ]);
        var btn = el("button", {
          className: "sb-action action-run",
          title: c.error ? "script has a parse error" : "Run " + label,
          onclick: function (e) { e.stopPropagation(); startRun(c.path); },
        }, ["\u25B6"]);
        if (c.error) btn.setAttribute("disabled", "disabled");
        li.appendChild(btn);
        list.appendChild(li);
      });
  }

  function refreshSessions() {
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (data) {
      dispatch({ type: "SESSIONS_SYNCED", list: data || [] });
    });
  }

  // 命令栏只能挑池里还活着的会话
  function syncSessionSelect() {
    if (!refs.liveTail || !refs.liveTail.updateSessionSelect) return;
    var pool = {};
    Object.keys(state.sessions).forEach(function (sid) {
      if (state.sessions[sid].live) pool[sid] = true;
    });
    refs.liveTail.updateSessionSelect(pool);
  }

  function renderSessions() {
    var list = document.getElementById("sess-list");
    list.innerHTML = "";
    // 池里的会话 + 主区数据里出现过的历史会话，后者用空心点区分
    var ids = Object.keys(state.sessions);
    if (!ids.length) {
      list.appendChild(el("li", { className: "hint" }, ["(none)"]));
      return;
    }
    ids.forEach(function (sid) {
      var live = !!state.sessions[sid].live;
      var label = (live ? "\u25CF " : "\u25CB ") + state.sessions[sid].label;
      if (!state.split.enabled) {
        list.appendChild(el("li", { className: "sb-item", title: sid }, [
          el("span", { className: "sb-label" }, [label]),
        ]));
        return;
      }
      var on = state.split.order.indexOf(sid) >= 0;
      var li = el("li", {
        className: "sb-item sess-pick" + (on ? " active" : ""),
        title: sid + (on ? "\n\n已分栏显示" : "\n\n点一下加一列"),
        onclick: function () { toggleSessionPane(sid); },
      }, [
        el("span", { className: "sb-check" }, [on ? "\u2611" : "\u2610"]),
        el("span", { className: "sb-label" }, [label]),
      ]);
      list.appendChild(li);
    });
  }

  function runActionButton(r) {
    // 按钮形态完全由 run 状态决定：排队/在跑 = ⏹ 停，终态 = ⟳ 重跑
    if (state.stopping[r.id] && r.status === "running") {
      var b = el("button", { className: "sb-action action-stopping", disabled: "disabled" }, ["stopping\u2026"]);
      return b;
    }
    if (r.status === "queued") {
      return el("button", {
        className: "sb-action action-stop", title: "从队列里移除",
        onclick: function (e) { e.stopPropagation(); cancelRun(r.id); },
      }, ["\u23F9"]);
    }
    if (r.status === "running") {
      return el("button", {
        className: "sb-action action-stop", title: "请求停止（下一条命令边界生效）",
        onclick: function (e) { e.stopPropagation(); stopRun(r.id); },
      }, ["\u23F9"]);
    }
    return el("button", {
      className: "sb-action action-rerun", title: "用同一脚本重跑",
      onclick: function (e) { e.stopPropagation(); rerun(r.id); },
    }, ["\u27F3"]);
  }

  function refreshRuns() {
    // 活跃日志走 /api/runs（含队列态）；历史日志走 /api/logs/{id}/runs
    var url = (state.viewedLogId && state.viewedLogId !== state.currentLogId)
      ? "/api/logs/" + encodeURIComponent(state.viewedLogId) + "/runs"
      : "/api/runs";
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      dispatch({ type: "RUNS_LOADED", runs: data || [] });
    });
  }

  function renderRuns() {
    var list = document.getElementById("runs-list");
    list.innerHTML = "";
    if (!state.runs.length) {
      list.appendChild(el("li", { className: "hint" }, ["(none)"]));
      return;
    }
    state.runs.slice().reverse().forEach(function (r) {
      // 主区看的是哪个 run 才高亮；正在跑的那条不自动抢占主区
      var cls = "sb-item" + (r.id === state.viewedRunId && state.view === "run" ? " active" : "");
      var li = el("li", { className: cls, title: r.script_path || r.id }, [
        el("span", {
          className: "sb-label",
          onclick: function () { showRunDetail(r.id); },
        }, [statusIcon(r.status) + " " + r.id]),
        el("span", { className: "badge status-" + r.status }, [r.status]),
      ]);
      li.appendChild(runActionButton(r));
      list.appendChild(li);
    });
  }

  // 主区正在看的 run 状态变了 → 同步头部（不改变用户所在视图）
  function syncTimelineMeta() {
    if (state.view !== "run" || !refs.timeline) return;
    var cur = state.runsById[state.viewedRunId];
    if (!cur) return;
    refs.timeline.setMeta({
      status: cur.status,
      ended_at: cur.ended_at,
      exception: cur.exception ? { type: "Exception", message: cur.exception } : null,
    });
  }

  // -------------------------------------------------- run 指令

  function startRun(path) {
    fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path }),
    }).then(function (r) { return r.json(); }).then(function () {
      refreshRuns();
    });
  }

  function stopRun(rid) {
    // 乐观更新：按下即置灰 + stopping…，等 run.finished 事件才复位；
    // 请求被拒或网络挂了就 dispatch 回滚
    dispatch({ type: "RUN_STOP_REQUESTED", rid: rid });
    fetch("/api/runs/" + encodeURIComponent(rid) + "/stop", { method: "POST" })
      .then(function (r) {
        if (!r.ok) dispatch({ type: "RUN_STOP_FAILED", rid: rid });
      })
      .catch(function () { dispatch({ type: "RUN_STOP_FAILED", rid: rid }); })
      .then(function () { refreshRuns(); });
  }

  function cancelRun(rid) {
    fetch("/api/runs/" + encodeURIComponent(rid), { method: "DELETE" })
      .then(function () { refreshRuns(); });
  }

  function rerun(rid) {
    // 不弹确认：重跑是幂等的低风险操作，多一次点击反而碍事
    fetch("/api/runs/" + encodeURIComponent(rid) + "/rerun", { method: "POST" })
      .then(function () { refreshRuns(); });
  }

  // -------------------------------------------------- 日志组

  function refreshLogs() {
    fetch("/api/logs").then(function (r) { return r.json(); }).then(function (data) {
      dispatch({ type: "LOGS_LOADED", logs: data || [] });
    });
  }

  // liveBuffer 挂钩活跃日志：日志一换就重置
  function syncLiveLog() {
    var active = activeLog();
    if (!active) return;
    if (refs.liveBufferLogId !== active.id) resetLiveBuffer(active.id);
    else if (refs.liveTail) refs.liveTail.setLog(active.name);
  }

  function renderLogSwitcher() {
    var nameEl = document.getElementById("log-current-name");
    var countEl = document.getElementById("log-current-count");
    var pinBtn = document.getElementById("log-pin-btn");
    var active = activeLog();
    if (active) {
      nameEl.textContent = (active.pinned ? "\u2605 " : "") + active.name;
      countEl.textContent = active.run_count + " runs";
      pinBtn.classList.toggle("active", !!active.pinned);
    } else {
      nameEl.textContent = "(none)";
      countEl.textContent = "";
    }
    var dd = document.getElementById("log-dropdown");
    dd.innerHTML = "";
    var sorted = state.logs.slice().sort(function (a, b) {
      if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      return b.created_at - a.created_at;
    });
    sorted.forEach(function (l) {
      var row = el("div", {
        className: "log-item" + (l.id === state.viewedLogId ? " active" : ""),
        onclick: function (e) { e.stopPropagation(); switchToLog(l.id); },
      }, []);
      row.appendChild(el("span", { className: "log-item-name" }, [
        (l.pinned ? "\u2605 " : "") + l.name + (l.is_active ? "  (active)" : ""),
      ]));
      row.appendChild(el("span", { className: "log-item-meta hint" }, [l.run_count + " runs"]));
      if (!l.is_active) {
        row.appendChild(el("button", {
          className: "log-item-del",
          title: "discard (hard delete)",
          onclick: function (e) {
            e.stopPropagation();
            if (!confirm("Discard log \"" + l.name + "\"? This cannot be undone.")) return;
            discardLog(l.id);
          },
        }, ["\u00D7"]));
      }
      dd.appendChild(row);
    });
  }

  function switchToLog(logId) {
    // 只切 sidebar Runs 的浏览范围；活跃日志（写入目标）不变，Live 也不断
    dispatch({ type: "LOG_SELECTED", logId: logId });
    document.getElementById("log-dropdown").hidden = true;
    if (state.view === "run") showLive();
    refreshRuns();
  }

  function rotateLog() {
    fetch("/api/logs/rotate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }).then(function (r) {
      if (r.status === 409) {
        return r.json().then(function (err) {
          alert("Cannot start a new log:\n\n" + (err.detail || "a run is in progress")
            + "\n\n请等待当前 run 跑完，或先停掉它，再试。");
          throw new Error("conflict");
        });
      }
      return r.json();
    }).then(function (newLog) {
      dispatch({ type: "LOG_ROTATED", logId: newLog.id });
      resetLiveBuffer(newLog.id);
      showLive();
      refreshLogs();
      refreshRuns();
    }).catch(function () { /* alert 已弹 */ });
  }

  function renameLog() {
    var active = activeLog();
    if (!active) return;
    var name = prompt("New name for current log:", active.name);
    if (name == null) return;
    name = name.trim();
    if (!name) return;
    fetch("/api/logs/" + encodeURIComponent(active.id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    }).then(function () { refreshLogs(); });
  }

  function togglePinCurrent() {
    var active = activeLog();
    if (!active) return;
    fetch("/api/logs/" + encodeURIComponent(active.id) + "/pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned: !active.pinned }),
    }).then(function () { refreshLogs(); });
  }

  function discardLog(logId) {
    fetch("/api/logs/" + encodeURIComponent(logId), { method: "DELETE" })
      .then(function (r) {
        if (r.status === 409) {
          return r.json().then(function (err) {
            alert("Cannot discard: " + (err.detail || "conflict"));
            throw new Error("conflict");
          });
        }
      })
      .then(function () {
        dispatch({ type: "LOG_DISCARDED", logId: logId });
        refreshLogs();
        refreshRuns();
      })
      .catch(function () { /* ignore */ });
  }

  // -------------------------------------------------- WebSocket

  function onWsEvent(ev) {
    if (!ev || !ev.type) return;
    // run.record = SessionLogRecord；run.meta = workspace.run.begin/end 边界元行
    if (ev.type === "run.record" || ev.type === "run.meta") {
      pushLive(ev.record, ev.run_id);
      // 主区正在回放这个 run（且它还在跑）→ 同步喂给 timeline
      if (state.view === "run" && state.viewedRunId === ev.run_id && refs.timeline) {
        refs.timeline.appendLive(ev.record);
      }
      return;
    }
    if (ev.type === "run.finished") {
      dispatch({ type: "RUN_FINISHED", rid: ev.run_id });
      refreshRuns();
      refreshSessions();
      refreshLogs();
      return;
    }
    if (ev.type === "run.enqueued" || ev.type === "run.started"
        || ev.type === "run.cancelled" || ev.type === "run.stopping") {
      refreshRuns();
      refreshSessions();
      return;
    }
    if (ev.type === "log.rotated") {
      dispatch({ type: "LOG_ROTATED", logId: ev.log_id });
      resetLiveBuffer(ev.log_id);
      showLive();
      refreshLogs();
      refreshRuns();
      return;
    }
    if (ev.type === "log.renamed" || ev.type === "log.pinned" || ev.type === "log.discarded") {
      refreshLogs();
      return;
    }
    // 手动命令执行事件
    if (ev.type === "cmd.output") {
      // 手动通道也走 LiveBody；__rid 保持 null，LiveTail 会据此打 ev-manual 标。
      // 保留服务端带来的 event / level，让"成功 command_end 隐藏"等规则同样生效。
      var rec = {
        timestamp: Date.now() / 1000,
        sequence: 0,
        session_id: ev.session_id,
        event: ev.event || "command_output",
        level: ev.level || "INFO",
        stream: ev.stream || "stdout",
        message: ev.data || "",
        operation_id: ev.command_id
      };
      pushLive(rec, null);
      return;
    }
    if (ev.type === "cmd.finished") {
      var cmdInfo = window.__cmdRunning && window.__cmdRunning[ev.command_id];
      if (cmdInfo) {
        // 保存到 LiveTail 的历史
        if (refs.liveTail && refs.liveTail._cmdHistory) {
          var sid = cmdInfo.sessionId;
          if (!refs.liveTail._cmdHistory[sid]) {
            refs.liveTail._cmdHistory[sid] = [];
          }
          refs.liveTail._cmdHistory[sid].push({
            command: cmdInfo.command,
            timestamp: cmdInfo.startTime,
            exit_code: ev.exit_code,
            duration: ev.duration
          });
          // 限制历史条数
          if (refs.liveTail._cmdHistory[sid].length > 100) {
            refs.liveTail._cmdHistory[sid] = refs.liveTail._cmdHistory[sid].slice(-100);
          }
          // 持久化到 localStorage
          refs.liveTail._saveHistoryToStorage();
        }
        delete window.__cmdRunning[ev.command_id];
      }
      // 刷新命令输入框状态
      var cmdBar = document.querySelector(".command-bar");
      if (cmdBar) cmdBar.classList.remove("running");
      var cmdInput = document.getElementById("cmd-input");
      if (cmdInput) cmdInput.disabled = false;
      return;
    }
    if (ev.type === "cmd.error") {
      // 显示错误
      console.error("Command error:", ev.error);
      var cmdBar = document.querySelector(".command-bar");
      if (cmdBar) cmdBar.classList.remove("running");
      var cmdInput = document.getElementById("cmd-input");
      if (cmdInput) cmdInput.disabled = false;
      return;
    }
  }

  function connectWs() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var url = proto + "://" + location.host + "/ws";
    try {
      var sock = new WebSocket(url);
      sock.onmessage = function (e) {
        try { onWsEvent(JSON.parse(e.data)); } catch (err) { /* ignore */ }
      };
      sock.onclose = function () { setTimeout(connectWs, 2000); };
    } catch (e) { /* ignore */ }
  }

  // -------------------------------------------------- 订阅者：state → DOM
  //
  // 每个订阅者靠 slice 的引用比较自己判脏：reducer 只给真正变了的分支换新对象，
  // 所以 `next.x !== prev.x` 就是一次廉价的 dirty check。空转的轮询不会重绘。

  store.subscribe(function (next, prev, action) {
    if (next.sessions !== prev.sessions || next.split !== prev.split) {
      renderSessions();
      syncSessionSelect();
      // 用户自己点的要立刻响应；数据驱动的攒一拍，避免一次 run 里连开好几列
      schedulePanes(action.type === "PANE_TOGGLE"
        || action.type === "SPLIT_TOGGLE"
        || action.type === "SPLIT_PREF_LOADED");
    }
    if (next.split !== prev.split) {
      syncSplitButton();
      persistSplit();
    }
    if (next.runs !== prev.runs || next.stopping !== prev.stopping
        || next.view !== prev.view || next.viewedRunId !== prev.viewedRunId) {
      renderRuns();
    }
    if (next.runsById !== prev.runsById) syncTimelineMeta();
    if (next.scripts !== prev.scripts) renderScriptList();
    if (next.logs !== prev.logs || next.viewedLogId !== prev.viewedLogId) {
      renderLogSwitcher();
    }
    if (next.logs !== prev.logs || next.currentLogId !== prev.currentLogId) {
      syncLiveLog();
    }
  });

  // -------------------------------------------------- 绑定 + 启动

  document.getElementById("log-current-btn").addEventListener("click", function (e) {
    e.stopPropagation();
    var dd = document.getElementById("log-dropdown");
    dd.hidden = !dd.hidden;
  });
  document.getElementById("log-rename-btn").addEventListener("click", renameLog);
  document.getElementById("log-pin-btn").addEventListener("click", togglePinCurrent);
  document.getElementById("log-rotate-btn").addEventListener("click", rotateLog);
  document.getElementById("back-to-live").addEventListener("click", showLive);
  document.getElementById("split-toggle").addEventListener("click", toggleSplit);
  document.addEventListener("click", function () {
    var dd = document.getElementById("log-dropdown");
    if (dd) dd.hidden = true;
  });

  // devtools 入口：__rpmTraceActions = true 打开操作流日志，
  // __rpmStore.getState() 看当前事务态
  window.__rpmTraceActions = false;
  window.__rpmStore = store;

  loadSplitPref();
  syncSplitButton();
  ensureLiveTail();
  // 首屏：列表都还空着，先照当前 state 画一次，后面全由订阅者驱动
  renderScriptList();
  renderSessions();
  renderRuns();
  renderLogSwitcher();
  refreshScripts();
  refreshSessions();
  refreshLogs();
  refreshRuns();
  connectWs();
  // 兜底轮询：WS 断开时仍保持列表新鲜度
  setInterval(refreshSessions, 5000);
  setInterval(refreshRuns, 3000);
  setInterval(refreshLogs, 8000);
})();
</script>
</body>
</html>
"""


__all__ = ["build_app"]
