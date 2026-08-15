// RedPyMake 前端时间轴 (CORE-11 §web)
//
// 提供两种模式：
//   1) Live  —— 追随 tail：新 record 到达就 append 到底，playhead = ∞；
//   2) Replay —— 用 wall-clock * speed 推进 playhead，只显示 timestamp <= playhead
//      的 record。支持 ▶/⏸/⏮/Step/Speed/Scrubber，任何拖动 scrubber 都会
//      自动切到 Replay 并暂停。
//
// 两种模式的日志行都落到 PaneSet：单列时就是一个 ol.timeline-body，分栏时按
// record.session_id 分发到多列。时钟与工具条始终只有一份，所以分栏回放天然同步。
//
// 静态报告场景（report HTML）：读 <script id="run-data"> 里内嵌 JSON，
// 一次性构造 timeline，默认进入 Replay 且从头播放（浏览器打开即回放）。
// Serve 场景：由外层脚本通过 window.__RedPyMakeTimeline.mount() 挂载。

(function () {
  "use strict";

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === "className") n.className = attrs[k];
        else if (k === "onclick") n.onclick = attrs[k];
        else if (k === "oninput") n.oninput = attrs[k];
        else if (k === "onchange") n.onchange = attrs[k];
        else n.setAttribute(k, attrs[k]);
      }
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      if (typeof c === "string" || typeof c === "number") {
        n.appendChild(document.createTextNode(String(c)));
      } else {
        n.appendChild(c);
      }
    });
    return n;
  }

  function fmtDurationSec(sec) {
    if (typeof sec !== "number" || !isFinite(sec) || sec < 0) sec = 0;
    var m = Math.floor(sec / 60);
    var s = sec - 60 * m;
    var mm = m < 10 ? "0" + m : String(m);
    var ss = s.toFixed(2);
    if (parseFloat(ss) < 10) ss = "0" + ss;
    return mm + ":" + ss;
  }

  function fmtWall(ts) {
    if (typeof ts !== "number") return "-";
    var d = new Date(ts * 1000);
    var hh = d.getHours(), mm = d.getMinutes(), ss = d.getSeconds();
    var ms = d.getMilliseconds();
    function pad(n, w) { n = String(n); while (n.length < w) n = "0" + n; return n; }
    return pad(hh, 2) + ":" + pad(mm, 2) + ":" + pad(ss, 2) + "." + pad(ms, 3);
  }

  function severityClass(level) {
    if (level === "ERROR" || level === "CRITICAL") return "lvl-err";
    if (level === "WARNING") return "lvl-warn";
    if (level === "DEBUG") return "lvl-debug";
    return "lvl-info";
  }

  // ---------- PaneSet ----------
  //
  // 日志行的落地容器。单列时就是今天那个 ol.timeline-body；分栏时按
  // record.session_id 把行分发到各列。时钟、工具条、header 都留在宿主组件
  // （Timeline / LiveTail）里只有一份——分栏共享同一个播放头就是靠这个。
  //
  // 没匹配上任何列的记录（脚本自身的 user_log、run 分隔条）进 fallback 列。

  var SCRIPT_PANE_ID = "__script__";

  function PaneSet(container, options) {
    this.container = container;
    this.maxRows = (options && options.maxRows) || 0;
    this.bodyClass = (options && options.bodyClass) || "";
    this.panes = [];
    this.byId = {};
    this.fallback = null;
    this.setPanes([]);
  }

  PaneSet.prototype.isSplit = function () {
    return this.panes.length > 1;
  };

  PaneSet.prototype._makeBody = function () {
    var cls = "timeline-body" + (this.bodyClass ? " " + this.bodyClass : "");
    var body = el("ol", { className: cls }, []);
    var pane = { body: body, userScrolled: false };
    body.addEventListener("scroll", function () {
      var near = (body.scrollTop + body.clientHeight) >= (body.scrollHeight - 8);
      pane.userScrolled = !near;
    });
    return pane;
  };

  // defs: [] / null → 单列；否则每个 {id, label} 一列，外加一个 Script 兜底列
  PaneSet.prototype.setPanes = function (defs) {
    var self = this;
    this.container.innerHTML = "";
    this.panes = [];
    this.byId = {};
    if (!defs || !defs.length) {
      var only = this._makeBody();
      only.id = null;
      this.container.classList.remove("split");
      this.container.appendChild(only.body);
      this.panes = [only];
      this.fallback = only;
      return;
    }
    this.container.classList.add("split");
    defs.concat([{ id: SCRIPT_PANE_ID, label: "Script" }]).forEach(function (def) {
      var pane = self._makeBody();
      pane.id = def.id;
      var wrap = el("div", {
        className: "pane" + (def.id === SCRIPT_PANE_ID ? " pane-script" : ""),
      }, [
        el("div", { className: "pane-header", title: def.id }, [def.label || def.id]),
      ]);
      wrap.appendChild(pane.body);
      self.container.appendChild(wrap);
      self.panes.push(pane);
      self.byId[def.id] = pane;
      if (def.id === SCRIPT_PANE_ID) self.fallback = pane;
    });
  };

  PaneSet.prototype.paneFor = function (sessionId) {
    if (!this.isSplit()) return this.panes[0];
    var hit = sessionId ? this.byId[sessionId] : null;
    return hit || this.fallback;
  };

  PaneSet.prototype.append = function (sessionId, node) {
    var pane = this.paneFor(sessionId);
    if (!pane) return;
    pane.body.appendChild(node);
    if (this.maxRows) {
      while (pane.body.childElementCount > this.maxRows) {
        pane.body.removeChild(pane.body.firstElementChild);
      }
    }
  };

  PaneSet.prototype.clear = function () {
    this.panes.forEach(function (p) { p.body.innerHTML = ""; });
  };

  // 只推没被用户滚离底部的列
  PaneSet.prototype.autoScroll = function () {
    this.panes.forEach(function (p) {
      if (p.userScrolled) return;
      p.body.scrollTop = p.body.scrollHeight;
    });
  };

  PaneSet.prototype.scrollToEnd = function () {
    this.panes.forEach(function (p) {
      p.userScrolled = false;
      p.body.scrollTop = p.body.scrollHeight;
    });
  };

  // ---------- Timeline component ----------

  function Timeline(root) {
    this.root = root;
    this.records = [];        // 全量 (含 script.begin/end)
    this.visible = [];        // 已渲染 record 引用（当前 mode 下）
    this.meta = { name: "", started_at: null, ended_at: null, exception: null, status: null };
    this.mode = "live";       // "live" | "replay"
    this.playing = false;     // replay 模式下是否推进
    this.speed = 1;
    this.playheadTs = 0;      // replay 播放头（seconds since epoch）
    this._wallAnchor = 0;     // wall clock anchor 与 playheadTs 对齐
    this._tickTimer = null;
    this._nodes = {};
    this.paneSet = null;      // _buildDom 里建；日志行的落地容器（单列或分栏）
    this._buildDom();
  }

  Timeline.prototype._buildDom = function () {
    var self = this;
    var header = el("div", { className: "run-header" }, []);
    var title = el("strong", { className: "run-title" }, ["(no run)"]);
    var status = el("span", { className: "run-status hint" }, []);
    header.appendChild(title);
    header.appendChild(status);

    var toolbar = el("div", { className: "timeline-toolbar" }, []);
    var btnLive = el("button", { className: "tb-btn active", onclick: function () { self.setLive(); } }, ["Live"]);
    var btnPlay = el("button", { className: "tb-btn", onclick: function () { self.togglePlay(); } }, ["▶"]);
    var btnReset = el("button", { className: "tb-btn", onclick: function () { self.reset(); } }, ["⏮"]);
    var btnStep = el("button", { className: "tb-btn", onclick: function () { self.step(); } }, ["⏭"]);

    var speed = el("select", { className: "tb-speed", onchange: function (e) { self.setSpeed(parseFloat(e.target.value)); } }, [
      el("option", { value: "0.5" }, ["0.5x"]),
      el("option", { value: "1", selected: "selected" }, ["1x"]),
      el("option", { value: "2" }, ["2x"]),
      el("option", { value: "4" }, ["4x"]),
      el("option", { value: "10" }, ["10x"]),
      el("option", { value: "9999" }, ["Max"]),
    ]);

    var scrubber = el("input", {
      type: "range", min: "0", max: "1000", value: "0", step: "1",
      className: "tb-scrubber",
      oninput: function (e) { self.onScrub(parseInt(e.target.value, 10)); },
    });

    var timeLabel = el("span", { className: "tb-time hint" }, ["00:00.00 / 00:00.00"]);

    toolbar.appendChild(btnLive);
    toolbar.appendChild(btnPlay);
    toolbar.appendChild(btnReset);
    toolbar.appendChild(btnStep);
    toolbar.appendChild(el("span", { className: "tb-label" }, ["Speed"]));
    toolbar.appendChild(speed);
    toolbar.appendChild(scrubber);
    toolbar.appendChild(timeLabel);

    var paneWrap = el("div", { className: "pane-set" }, []);

    this.root.innerHTML = "";
    this.root.appendChild(header);
    this.root.appendChild(toolbar);
    this.root.appendChild(paneWrap);

    this.paneSet = new PaneSet(paneWrap);

    this._nodes = {
      header: header, title: title, status: status,
      btnLive: btnLive, btnPlay: btnPlay, btnReset: btnReset, btnStep: btnStep,
      speed: speed, scrubber: scrubber, timeLabel: timeLabel,
      paneWrap: paneWrap,
    };
  };

  // 分栏：只换落地容器，playhead / 工具条不动 → 多列共享同一个播放时钟
  Timeline.prototype.setPanes = function (defs) {
    this.paneSet.setPanes(defs);
    this._fullRender();
  };

  Timeline.prototype._dataStart = function () {
    return this.meta.started_at || (this.records.length ? this.records[0].timestamp : 0);
  };
  Timeline.prototype._dataEnd = function () {
    if (this.meta.ended_at) return this.meta.ended_at;
    if (this.records.length) return this.records[this.records.length - 1].timestamp;
    return this._dataStart();
  };
  Timeline.prototype._duration = function () {
    return Math.max(0, this._dataEnd() - this._dataStart());
  };

  Timeline.prototype.load = function (payload) {
    var meta = (payload && payload.meta) || {};
    var records = (payload && payload.records) || [];
    this.meta = {
      name: meta.name || "(unnamed)",
      started_at: meta.started_at || null,
      ended_at: meta.ended_at || null,
      exception: meta.exception || null,
      status: meta.status || null,
    };
    this.records = records.slice();
    this.paneSet.scrollToEnd();
    // 未结束的 run → 默认 Live；已结束 → 默认 Replay 从头播
    if (this._isRunning()) {
      this.setLive();
    } else {
      this.mode = "replay";
      this._nodes.btnLive.classList.remove("active");
      this.playheadTs = this._dataStart();
      this.playing = true;
      this._wallAnchor = performance.now() / 1000;
      this._render();
      this._ensureTick();
    }
    this._updateHeader();
  };

  Timeline.prototype.appendLive = function (record) {
    this.records.push(record);
    if (this.mode === "live") {
      this._appendNode(record);
      this._maybeAutoScroll();
    } else {
      // Replay 模式下，新 record 不会立即出现——playhead 到达时才显示
      this._updateScrubberBounds();
    }
    this._updateTimeLabel();
  };

  Timeline.prototype.setMeta = function (partial) {
    for (var k in partial) if (partial.hasOwnProperty(k)) this.meta[k] = partial[k];
    this._updateHeader();
  };

  Timeline.prototype._isRunning = function () {
    if (this.meta.status === "running" || this.meta.status === "queued") return true;
    return !this.meta.ended_at;
  };

  Timeline.prototype._updateHeader = function () {
    this._nodes.title.textContent = this.meta.name || "(unnamed)";
    var s = this._nodes.status;
    s.textContent = "";
    s.className = "run-status";
    if (this.meta.exception) {
      s.classList.add("status-failed");
      s.textContent = "✗ " + (this.meta.exception.type || "Exception") + ": " + (this.meta.exception.message || "");
    } else if (this.meta.status === "running") {
      s.classList.add("status-running");
      s.textContent = "● running";
    } else if (this.meta.status === "queued") {
      s.classList.add("status-queued");
      s.textContent = "⏸ queued";
    } else if (this.meta.ended_at) {
      s.classList.add("status-succeeded");
      s.textContent = "✓ succeeded";
    } else {
      s.classList.add("hint");
      s.textContent = "(idle)";
    }
    this._updateTimeLabel();
  };

  Timeline.prototype._updateScrubberBounds = function () {
    // scrubber value: 0..1000 mapped linearly to [start, end]
    var frac = 0;
    var dur = this._duration();
    if (dur > 0) {
      frac = (this.playheadTs - this._dataStart()) / dur;
    }
    if (this.mode === "live") frac = 1;
    frac = Math.max(0, Math.min(1, frac));
    this._nodes.scrubber.value = String(Math.round(frac * 1000));
  };

  Timeline.prototype._updateTimeLabel = function () {
    var dur = this._duration();
    var pos;
    if (this.mode === "live") {
      pos = dur;
    } else {
      pos = Math.max(0, Math.min(dur, this.playheadTs - this._dataStart()));
    }
    this._nodes.timeLabel.textContent = fmtDurationSec(pos) + " / " + fmtDurationSec(dur);
    this._updateScrubberBounds();
  };

  // ---------- controls ----------

  Timeline.prototype.setLive = function () {
    this.mode = "live";
    this.playing = false;
    this._nodes.btnLive.classList.add("active");
    this._nodes.btnPlay.textContent = "▶";
    this.playheadTs = Infinity;
    this._fullRender();
    this.paneSet.scrollToEnd();
    this._updateTimeLabel();
    this._stopTick();
  };

  Timeline.prototype.enterReplay = function () {
    if (this.mode !== "replay") {
      this.mode = "replay";
      this._nodes.btnLive.classList.remove("active");
      // 记录当前"live 展现"的时间戳作为切入点，避免视图跳动
      var end = this._dataEnd();
      this.playheadTs = end;
    }
  };

  Timeline.prototype.togglePlay = function () {
    this.enterReplay();
    this.playing = !this.playing;
    this._nodes.btnPlay.textContent = this.playing ? "⏸" : "▶";
    if (this.playing) {
      // 若播放头在末尾（且已结束），从头再来
      if (this.playheadTs >= this._dataEnd() && !this._isRunning()) {
        this.playheadTs = this._dataStart();
        this._render();
      }
      this._wallAnchor = performance.now() / 1000;
      this._ensureTick();
    } else {
      this._stopTick();
    }
  };

  Timeline.prototype.reset = function () {
    this.enterReplay();
    this.playheadTs = this._dataStart();
    this.playing = false;
    this._nodes.btnPlay.textContent = "▶";
    this._render();
    this._stopTick();
    this._updateTimeLabel();
  };

  Timeline.prototype.step = function () {
    this.enterReplay();
    this.playing = false;
    this._nodes.btnPlay.textContent = "▶";
    // 下一条 timestamp > playheadTs 的 record
    var next = null;
    for (var i = 0; i < this.records.length; i++) {
      var r = this.records[i];
      if (this._isMeta(r)) continue;
      if (typeof r.timestamp !== "number") continue;
      if (r.timestamp > this.playheadTs + 1e-6) { next = r; break; }
    }
    if (next) this.playheadTs = next.timestamp;
    else this.playheadTs = this._dataEnd();
    this._render();
    this._updateTimeLabel();
  };

  Timeline.prototype.setSpeed = function (v) {
    if (isNaN(v) || v <= 0) v = 1;
    this.speed = v;
    if (this.playing) this._wallAnchor = performance.now() / 1000;
  };

  Timeline.prototype.onScrub = function (val) {
    var frac = val / 1000;
    this.enterReplay();
    this.playheadTs = this._dataStart() + frac * this._duration();
    this._render();
    this._updateTimeLabel();
    // 拖动时暂停，若之前在播放则保持播放锚点更新
    if (this.playing) this._wallAnchor = performance.now() / 1000;
  };

  Timeline.prototype._ensureTick = function () {
    if (this._tickTimer) return;
    var self = this;
    this._tickTimer = setInterval(function () { self._tick(); }, 100);
  };

  Timeline.prototype._stopTick = function () {
    if (this._tickTimer) { clearInterval(this._tickTimer); this._tickTimer = null; }
  };

  Timeline.prototype._tick = function () {
    if (this.mode !== "replay" || !this.playing) return;
    var now = performance.now() / 1000;
    var deltaWall = now - this._wallAnchor;
    this._wallAnchor = now;
    var newHead = this.playheadTs + deltaWall * this.speed;
    var end = this._dataEnd();
    if (newHead >= end && !this._isRunning()) {
      newHead = end;
      this.playing = false;
      this._nodes.btnPlay.textContent = "▶";
      this._stopTick();
    }
    this.playheadTs = newHead;
    this._render();
    this._updateTimeLabel();
  };

  // ---------- render ----------

  Timeline.prototype._isMeta = function (r) {
    if (!r) return false;
    return r.event === "script.begin" || r.event === "script.end"
      || r.event === "workspace.run.begin" || r.event === "workspace.run.end";
  };

  Timeline.prototype._recordVisible = function (r) {
    if (this._isMeta(r)) return false;
    if (this.mode === "live") return true;
    if (typeof r.timestamp !== "number") return true;
    return r.timestamp <= this.playheadTs + 1e-9;
  };

  Timeline.prototype._nodeForRecord = function (r) {
    var line = el("li", { className: "tl-row " + severityClass(r.level) }, []);
    var t = el("span", { className: "tl-ts" }, [fmtWall(r.timestamp)]);
    var ev = el("span", { className: "tl-ev" }, ["[" + (r.event || "?") + "]"]);
    var sid = r.session_id ? el("span", { className: "tl-sid" }, [r.session_id]) : null;
    var msg = el("span", { className: "tl-msg" }, [r.message || ""]);
    line.appendChild(t);
    line.appendChild(ev);
    if (sid) line.appendChild(sid);
    line.appendChild(msg);
    return line;
  };

  Timeline.prototype._fullRender = function () {
    this.paneSet.clear();
    for (var i = 0; i < this.records.length; i++) {
      var r = this.records[i];
      if (!this._recordVisible(r)) continue;
      this.paneSet.append(r.session_id, this._nodeForRecord(r));
    }
  };

  Timeline.prototype._render = function () {
    // Replay 模式下每次 render 都是全量重绘（record 数量不大时可接受）
    this._fullRender();
  };

  Timeline.prototype._appendNode = function (r) {
    if (!this._recordVisible(r)) return;
    this.paneSet.append(r.session_id, this._nodeForRecord(r));
  };

  Timeline.prototype._maybeAutoScroll = function () {
    this.paneSet.autoScroll();
  };

  // ---------- LiveTail component ----------
  //
  // 主区的默认形态。和 Timeline 的区别：
  //   - 数据源挂钩**活跃日志**而非单个 run —— 跨 run 持续累积，切去看历史 run 再
  //     切回来不丢中间的记录；
  //   - 只追随尾部，没有 playback 控件（回放是 Run detail 态的事）；
  //   - workspace.run.begin/end 元行渲染成分隔条，而不是普通日志行。

  var LIVE_MAX_ROWS = 4000; // DOM 行数上限；超出从头部裁剪，避免长时间挂机吃内存

  function LiveTail(root) {
    this.root = root;
    this.logName = "";
    this.count = 0;              // 已渲染的日志行数（不含分隔条）
    this._runStarts = {};        // run_id -> started_at，用于 end 分隔条算时长
    this._nodes = {};
    this.paneSet = null;
    this._buildDom();
  }

  LiveTail.prototype._buildDom = function () {
    var header = el("div", { className: "run-header" }, []);
    var title = el("strong", { className: "run-title" }, ["Live"]);
    var status = el("span", { className: "run-status hint" }, ["waiting for activity…"]);
    header.appendChild(title);
    header.appendChild(status);

    var paneWrap = el("div", { className: "pane-set" }, []);

    this.root.innerHTML = "";
    this.root.appendChild(header);
    this.root.appendChild(paneWrap);

    this.paneSet = new PaneSet(paneWrap, {
      maxRows: LIVE_MAX_ROWS,
      bodyClass: "live-body",
    });
    this._nodes = { header: header, title: title, status: status, paneWrap: paneWrap };
  };

  // 分栏：换列后 buffer 要重放一遍才填得满，records 由调用方（挂钩活跃日志的
  // liveBuffer）提供
  LiveTail.prototype.setPanes = function (defs, records) {
    this.paneSet.setPanes(defs);
    this.load(records || []);
  };

  LiveTail.prototype.setLog = function (name) {
    this.logName = name || "";
    this._updateHeader();
  };

  // 换日志组时调：live buffer 挂钩活跃日志，日志一换就得从头开始
  LiveTail.prototype.clear = function () {
    this.count = 0;
    this._runStarts = {};
    this.paneSet.clear();
    this.paneSet.scrollToEnd();
    this._updateHeader();
  };

  LiveTail.prototype.load = function (records) {
    this.clear();
    (records || []).forEach(this.append, this);
  };

  LiveTail.prototype.append = function (r) {
    if (!r) return;
    if (r.event === "workspace.run.begin") {
      if (r.run_id) this._runStarts[r.run_id] = r.started_at || r.timestamp;
      this._appendNode(this._separatorNode(
        "▶ " + (r.script_name || r.run_id || "run") + " · " + (r.run_id || ""),
        "sep-begin"
      ), null);
      this._updateHeader();
      return;
    }
    if (r.event === "workspace.run.end") {
      var started = this._runStarts[r.run_id];
      var dur = (typeof started === "number" && typeof r.ended_at === "number")
        ? " (" + (r.ended_at - started).toFixed(1) + "s)"
        : "";
      var st = r.status || "finished";
      this._appendNode(this._separatorNode(
        (r.run_id || "run") + " · " + st + dur,
        "sep-end status-" + st
      ), null);
      this._updateHeader();
      return;
    }
    // sink 自己的 script.begin/end 与 workspace 元行重复，不再单独占一行
    if (r.event === "script.begin" || r.event === "script.end") return;
    this.count += 1;
    this._appendNode(this._nodeForRecord(r), r.session_id);
    this._updateHeader();
  };

  LiveTail.prototype._separatorNode = function (text, cls) {
    return el("li", { className: "run-separator " + (cls || "") }, [
      el("span", { className: "run-separator-text" }, [text]),
    ]);
  };

  LiveTail.prototype._nodeForRecord = Timeline.prototype._nodeForRecord;

  LiveTail.prototype._appendNode = function (node, sessionId) {
    // sessionId 为空（run 分隔条、脚本自身日志）→ 落 Script 兜底列
    this.paneSet.append(sessionId, node);
    this.paneSet.autoScroll();
  };

  // 隐藏状态下 clientHeight 为 0，自动跟随算不准；切回 Live 时显式对齐到底部
  LiveTail.prototype.scrollToEnd = function () {
    this.paneSet.scrollToEnd();
  };

  LiveTail.prototype._updateHeader = function () {
    this._nodes.title.textContent = this.logName ? "Live · " + this.logName : "Live";
    var s = this._nodes.status;
    s.className = "run-status hint";
    s.textContent = this.count
      ? this.count + " record" + (this.count === 1 ? "" : "s")
      : "waiting for activity…";
  };

  // ---------- public factory ----------

  function mount(root, payload) {
    if (!root) return null;
    var tl = new Timeline(root);
    if (payload) tl.load(payload);
    return tl;
  }

  function mountLive(root) {
    if (!root) return null;
    return new LiveTail(root);
  }

  function loadInlineData() {
    var node = document.getElementById("run-data");
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || node.innerText || "{}");
    } catch (e) {
      return null;
    }
  }

  function init() {
    var root = document.getElementById("timeline-root");
    if (!root) return;
    var data = loadInlineData();
    if (data) {
      mount(root, data);
    }
  }

  window.__RedPyMakeTimeline = {
    mount: mount,
    mountLive: mountLive,
    Timeline: Timeline,
    LiveTail: LiveTail,
    PaneSet: PaneSet,
    SCRIPT_PANE_ID: SCRIPT_PANE_ID,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
