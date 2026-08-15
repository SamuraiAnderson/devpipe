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
<script src="/static/timeline.js"></script>
<script>
(function () {
  // liveBuffer 挂钩**活跃日志**：跨 run 持续累积，切去看历史 run 不打断，
  // 切回 Live 依然能看到完整尾部。只有换日志组时才重置。
  var LIVE_BUFFER_MAX = 8000;

  var state = {
    view: "live",            // "live" | "run"
    viewedRunId: null,       // view === "run" 时主区在看哪个 run
    currentRunId: null,      // 服务端当前正在跑的 run（只影响 sidebar，不抢主区）
    liveTail: null,          // LiveTail 实例，常驻不销毁
    timeline: null,          // Run detail 的 Timeline 实例
    liveBuffer: [],
    liveBufferLogId: null,   // liveBuffer 属于哪个 log
    runsById: {},
    scriptsByPath: {},
    logs: [],
    currentLogId: null,
    viewedLogId: null,       // sidebar Runs 展示范围；默认 = currentLogId
    stopping: {},            // rid -> true：已按下 stop，等 run.finished 复位
    // 分栏：勾选的 session 各占一列，Live 与 Run detail 共用同一套列定义
    split: { enabled: false, sessionIds: [] },
    sessionsSeen: {},        // session_id -> label；池里的 + 数据里出现过的
    poolSessions: {},        // session_id -> true：当前还在池里（未关闭）
    _paneTimer: null,
  };

  var SPLIT_STORAGE_KEY = "rpm.split";

  function loadSplitPref() {
    try {
      var raw = window.localStorage.getItem(SPLIT_STORAGE_KEY);
      if (!raw) return;
      var saved = JSON.parse(raw);
      if (saved && typeof saved === "object") {
        state.split.enabled = !!saved.enabled;
        state.split.sessionIds = saved.sessionIds || [];
      }
    } catch (e) { /* localStorage 不可用（隐私模式等）→ 用默认值 */ }
  }

  function saveSplitPref() {
    try {
      window.localStorage.setItem(SPLIT_STORAGE_KEY, JSON.stringify(state.split));
    } catch (e) { /* ignore */ }
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

  // 记下见过的 session：池里的、以及主区数据里出现过的（历史 run 的会话不在池里）
  function noteSession(sid, label) {
    if (!sid || isScriptSession(sid)) return false;
    var known = Object.prototype.hasOwnProperty.call(state.sessionsSeen, sid);
    state.sessionsSeen[sid] = label || state.sessionsSeen[sid] || sid;
    if (known) return false;
    // 分栏开着时新冒出来的会话直接给一列，免得它悄悄落进 Script 兜底列
    if (state.split.enabled && state.split.sessionIds.indexOf(sid) < 0) {
      state.split.sessionIds.push(sid);
      saveSplitPref();
      schedulePanes();
    }
    return true;
  }

  function paneDefs() {
    if (!state.split.enabled) return [];
    var defs = [];
    state.split.sessionIds.forEach(function (sid) {
      if (isScriptSession(sid)) return;
      defs.push({ id: sid, label: state.sessionsSeen[sid] || sid });
    });
    return defs;   // 空数组 → PaneSet 退回单列
  }

  function applyPanes() {
    var defs = paneDefs();
    if (state.liveTail) state.liveTail.setPanes(defs, state.liveBuffer);
    if (state.timeline) state.timeline.setPanes(defs);
  }

  // 一次 run 里会连续冒出好几个新 session，攒一拍再重建列
  function schedulePanes() {
    if (state._paneTimer) return;
    state._paneTimer = setTimeout(function () {
      state._paneTimer = null;
      applyPanes();
      refreshSessions();
    }, 150);
  }

  function toggleSplit() {
    state.split.enabled = !state.split.enabled;
    if (state.split.enabled && !state.split.sessionIds.length) {
      state.split.sessionIds = Object.keys(state.sessionsSeen);
    }
    saveSplitPref();
    document.getElementById("split-toggle").classList.toggle("active", state.split.enabled);
    applyPanes();
    refreshSessions();
  }

  function toggleSessionPane(sid) {
    var idx = state.split.sessionIds.indexOf(sid);
    if (idx >= 0) state.split.sessionIds.splice(idx, 1);
    else state.split.sessionIds.push(sid);
    saveSplitPref();
    applyPanes();
    refreshSessions();
  }

  // -------------------------------------------------- 主区：Live tail / Run detail

  function ensureLiveTail() {
    if (!state.liveTail) {
      state.liveTail = window.__RedPyMakeTimeline.mountLive(document.getElementById("live-root"));
      var log = activeLog();
      if (log) state.liveTail.setLog(log.name);
      if (state.split.enabled) state.liveTail.setPanes(paneDefs(), state.liveBuffer);
    }
    return state.liveTail;
  }

  function pushLive(rec, rid) {
    if (!rec) return;
    // 打标便于 Run detail 直接从 buffer 里切片，省一次服务端往返
    if (rid) rec.__rid = rid;
    noteSession(rec.session_id);
    state.liveBuffer.push(rec);
    if (state.liveBuffer.length > LIVE_BUFFER_MAX) state.liveBuffer.shift();
    ensureLiveTail().append(rec);
  }

  function resetLiveBuffer(logId) {
    state.liveBuffer = [];
    state.liveBufferLogId = logId || null;
    var tail = ensureLiveTail();
    tail.clear();
    var log = activeLog();
    if (log) tail.setLog(log.name);
  }

  function showLive() {
    state.view = "live";
    state.viewedRunId = null;
    state.timeline = null;
    document.getElementById("timeline-root").hidden = true;
    document.getElementById("live-root").hidden = false;
    document.getElementById("main-toolbar").hidden = true;
    ensureLiveTail().scrollToEnd();
    refreshRuns();
  }

  function showRunDetail(rid) {
    state.view = "run";
    state.viewedRunId = rid;
    document.getElementById("live-root").hidden = true;
    document.getElementById("timeline-root").hidden = false;
    var toolbar = document.getElementById("main-toolbar");
    toolbar.hidden = false;
    document.getElementById("main-context").textContent = "Run detail · " + rid;

    var known = state.runsById[rid];
    var buffered = state.liveBuffer.filter(function (r) { return r.__rid === rid; });
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
      state.runsById[rid] = data.run;
      mountRunTimeline(data.run, data.records || []);
      refreshRuns();
    });
  }

  function mountRunTimeline(run, records) {
    // 历史 run 的会话早已不在池里，只能从记录里认出来，否则没法勾选分栏
    (records || []).forEach(function (r) { noteSession(r.session_id); });
    state.timeline = window.__RedPyMakeTimeline.mount(document.getElementById("timeline-root"), {
      meta: {
        name: (run.script_name || run.id) + "  ·  " + run.id,
        started_at: run.started_at,
        ended_at: run.ended_at,
        exception: run.exception ? { type: "Exception", message: run.exception } : null,
        status: run.status,
      },
      records: records,
    });
    state.timeline.setPanes(paneDefs());
    refreshSessions();
  }

  // -------------------------------------------------- Sidebar

  function refreshScripts() {
    fetch("/api/scripts").then(function (r) { return r.json(); }).then(function (data) {
      var list = document.getElementById("scripts-list");
      list.innerHTML = "";
      state.scriptsByPath = {};
      if (!data.length) {
        list.appendChild(el("li", { className: "hint" }, ["(none)"]));
        return;
      }
      data.forEach(function (c) {
        state.scriptsByPath[c.path] = c;
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
    });
  }

  function refreshSessions() {
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (data) {
      state.poolSessions = {};
      (data || []).forEach(function (s) {
        if (!s.closed) state.poolSessions[s.session_id] = true;
        state.sessionsSeen[s.session_id] = s.kind + " / " + s.label;
      });
      renderSessions();
    });
  }

  function renderSessions() {
    var list = document.getElementById("sess-list");
    list.innerHTML = "";
    // 池里的会话 + 主区数据里出现过的历史会话，后者用空心点区分
    var ids = Object.keys(state.sessionsSeen);
    if (!ids.length) {
      list.appendChild(el("li", { className: "hint" }, ["(none)"]));
      return;
    }
    ids.forEach(function (sid) {
      var live = !!state.poolSessions[sid];
      var label = (live ? "\u25CF " : "\u25CB ") + (state.sessionsSeen[sid] || sid);
      if (!state.split.enabled) {
        list.appendChild(el("li", { className: "sb-item", title: sid }, [
          el("span", { className: "sb-label" }, [label]),
        ]));
        return;
      }
      var on = state.split.sessionIds.indexOf(sid) >= 0;
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
      var list = document.getElementById("runs-list");
      list.innerHTML = "";
      if (!data.length) {
        list.appendChild(el("li", { className: "hint" }, ["(none)"]));
        return;
      }
      var running = null;
      data.slice().reverse().forEach(function (r) {
        state.runsById[r.id] = r;
        if (r.status === "running") running = r.id;
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
      state.currentRunId = running;
      // 主区正在看的 run 状态变了 → 同步头部（不改变用户所在视图）
      if (state.view === "run" && state.timeline) {
        var cur = state.runsById[state.viewedRunId];
        if (cur) {
          state.timeline.setMeta({
            status: cur.status,
            ended_at: cur.ended_at,
            exception: cur.exception ? { type: "Exception", message: cur.exception } : null,
          });
        }
      }
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
    // 尽力而为：按下即置灰 + stopping…，等 run.finished 事件才复位
    state.stopping[rid] = true;
    refreshRuns();
    fetch("/api/runs/" + encodeURIComponent(rid) + "/stop", { method: "POST" })
      .then(function (r) {
        if (!r.ok) {
          delete state.stopping[rid];
          refreshRuns();
        }
      })
      .catch(function () { delete state.stopping[rid]; refreshRuns(); });
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
      state.logs = data || [];
      var active = activeLog();
      if (active) {
        var switched = state.currentLogId && state.currentLogId !== active.id;
        state.currentLogId = active.id;
        if (!state.viewedLogId || switched) state.viewedLogId = active.id;
        // liveBuffer 挂钩活跃日志：日志一换就重置
        if (state.liveBufferLogId !== active.id) resetLiveBuffer(active.id);
        else if (state.liveTail) state.liveTail.setLog(active.name);
      }
      renderLogSwitcher();
    });
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
    state.viewedLogId = logId;
    document.getElementById("log-dropdown").hidden = true;
    if (state.view === "run") showLive();
    refreshRuns();
    renderLogSwitcher();
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
      state.viewedLogId = newLog.id;
      state.currentLogId = newLog.id;
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
        if (state.viewedLogId === logId) state.viewedLogId = state.currentLogId;
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
      if (state.view === "run" && state.viewedRunId === ev.run_id && state.timeline) {
        state.timeline.appendLive(ev.record);
      }
      return;
    }
    if (ev.type === "run.finished") {
      delete state.stopping[ev.run_id];
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
      state.viewedLogId = ev.log_id;
      state.currentLogId = ev.log_id;
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

  loadSplitPref();
  document.getElementById("split-toggle").classList.toggle("active", state.split.enabled);
  ensureLiveTail();
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
