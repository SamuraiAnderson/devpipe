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
    this._splitInstance = null;
    this._savedWidths = null;   // { paneId: percent }，宿主没接桥时的本地兜底
    this.onLoadWidths = null;   // 取宽度偏好；见 bindPaneWidths
    this.onResize = null;       // 拖动结束时回调，宿主拿去持久化
    this.setPanes([]);
  }

  PaneSet.prototype.isSplit = function () {
    return this.panes.length > 1;
  };

  function sameIds(panes, ids) {
    if (panes.length !== ids.length) return false;
    for (var i = 0; i < panes.length; i++) {
      if (panes[i].id !== ids[i]) return false;
    }
    return true;
  }

  // Split.js 要求 sizes 之和为 100；存下来的宽度可能只覆盖部分列（新列还没记录），
  // 缺的按均分补，最后整体归一化。
  function normalizeSizes(ids, saved) {
    var fallback = 100 / ids.length;
    var sizes = ids.map(function (id) {
      var v = saved[id];
      return (typeof v === "number" && isFinite(v) && v > 0) ? v : fallback;
    });
    var total = sizes.reduce(function (a, b) { return a + b; }, 0);
    if (!total) return ids.map(function () { return fallback; });
    return sizes.map(function (v) { return (v / total) * 100; });
  }

  // Live 与 Run detail 各有一个 PaneSet，共用宿主（server.py 的 IIFE）那份宽度偏好。
  // 每次 resync 都回宿主取最新值，避免 Timeline 反复挂载时读到过期副本。
  // 静态 report 场景没有宿主桥，静默退化为均分。
  function bindPaneWidths(paneSet) {
    if (typeof window === "undefined") return;
    paneSet.onLoadWidths = function () {
      return (typeof window.__rpmLoadWidths === "function") ? window.__rpmLoadWidths() : null;
    };
    paneSet.onResize = function (widths) {
      if (typeof window.__rpmSaveWidths === "function") window.__rpmSaveWidths(widths);
    };
  }

  PaneSet.prototype._makeBody = function () {
    var cls = "timeline-body" + (this.bodyClass ? " " + this.bodyClass : "");
    var body = el("ol", { className: cls }, []);
    var pane = { id: null, body: body, wrap: null, header: null, userScrolled: false };
    body.addEventListener("scroll", function () {
      var near = (body.scrollTop + body.clientHeight) >= (body.scrollHeight - 8);
      pane.userScrolled = !near;
    });
    return pane;
  };

  // defs: [] / null → 单列；否则每个 {id, label} 一列，外加一个 Script 兜底列。
  //
  // 按 id 做增量 diff，**不整棵重建**：留下来的列 DOM 身份不变，滚动位置、已渲染
  // 的日志行、Split.js 拖出来的宽度都原样保留。一次 run 里会连续冒出新 session，
  // 全量重建会让用户的视图每隔几秒抖一次。
  //
  // 返回 {added, removed}：调用方据此只给新列补灌历史数据。
  PaneSet.prototype.setPanes = function (defs) {
    var self = this;
    var wantSplit = !!(defs && defs.length);

    if (!wantSplit) {
      // 单列那个 body 没有 wrap，从分栏切回来复用不了，整体重建
      if (this.isSplit()) {
        this._destroySplit();
        this.container.innerHTML = "";
        this.panes = [];
        this.byId = {};
        this.fallback = null;
      }
      this.container.classList.remove("split");
      if (!this.panes.length) {
        var only = this._makeBody();
        only.id = null;
        this.container.appendChild(only.body);
        this.panes = [only];
        this.fallback = only;
      }
      return { added: [], removed: [] };
    }

    var all = defs.concat([{ id: SCRIPT_PANE_ID, label: "Script" }]);
    var nextIds = all.map(function (def) { return def.id; });

    // 列集合没变（顶多 label 变了）→ 不碰 DOM 也不重建 Split，
    // 否则每次轮询都会把用户拖出来的宽度打回默认值
    if (this.isSplit() && sameIds(this.panes, nextIds)) {
      all.forEach(function (def) {
        self.byId[def.id].header.textContent = def.label || def.id;
      });
      return { added: [], removed: [] };
    }

    if (!this.isSplit()) {
      this.container.innerHTML = "";
      this.panes = [];
      this.byId = {};
      this.fallback = null;
    }

    // 先撤掉旧 Split 实例：它会把 gutter 从容器里摘掉，
    // 否则下面重排 pane 时 gutter 会留在错误的位置上
    this._destroySplit();
    this.container.classList.add("split");

    var wanted = {};
    all.forEach(function (def) { wanted[def.id] = def; });

    var removed = [];
    Object.keys(this.byId).forEach(function (id) {
      if (wanted[id]) return;
      self.byId[id].wrap.remove();
      delete self.byId[id];
      removed.push(id);
    });

    var added = [];
    all.forEach(function (def) {
      var pane = self.byId[def.id];
      if (pane) {
        pane.header.textContent = def.label || def.id;
        return;
      }
      pane = self._makeBody();
      pane.id = def.id;
      pane.header = el("div", { className: "pane-header", title: def.id }, [def.label || def.id]);
      pane.wrap = el("div", {
        className: "pane" + (def.id === SCRIPT_PANE_ID ? " pane-script" : ""),
      }, [pane.header]);
      pane.wrap.appendChild(pane.body);
      self.byId[def.id] = pane;
      added.push(def.id);
    });

    // appendChild 一个已在 DOM 里的节点等于移动它，不会重建 —— 顺序对齐 defs
    this.panes = all.map(function (def) {
      var pane = self.byId[def.id];
      self.container.appendChild(pane.wrap);
      return pane;
    });
    this.fallback = this.byId[SCRIPT_PANE_ID];

    this._resyncSplit();
    return { added: added, removed: removed };
  };

  PaneSet.prototype._destroySplit = function () {
    if (!this._splitInstance) return;
    try {
      this._splitInstance.destroy();   // 摘掉 gutter + 清掉它写的 inline flex-basis
    } catch (e) { /* 容器已被清空时 Split 会抛，忽略 */ }
    this._splitInstance = null;
  };

  // 只在列集合变化后调用。宽度优先用宿主存下来的百分比，新列按均分补。
  // Split.js 缺席时（没引 vendor 脚本）静默降级为 flex 均分，不影响其它功能。
  PaneSet.prototype._resyncSplit = function () {
    if (!this.isSplit() || typeof Split !== "function") return;
    var self = this;
    var wraps = this.panes.map(function (p) { return p.wrap; });
    var ids = this.panes.map(function (p) { return p.id; });
    var saved = (typeof this.onLoadWidths === "function" && this.onLoadWidths())
      || this._savedWidths || {};
    this._splitInstance = Split(wraps, {
      sizes: normalizeSizes(ids, saved),
      minSize: 120,
      gutterSize: 4,
      onDragEnd: function (newSizes) {
        var out = {};
        ids.forEach(function (id, i) { out[id] = newSizes[i]; });
        self._savedWidths = out;
        if (typeof self.onResize === "function") self.onResize(out);
      },
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
    bindPaneWidths(this.paneSet);

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
    this._paneFilter = null;     // 非空时只往这些列补行（分栏新增列的 buffer 重放）
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

    // 命令输入栏
    var commandBar = el("div", { className: "command-bar" }, [
      el("div", { className: "command-bar-left" }, [
        el("select", { id: "cmd-session-select", className: "command-session-select" }, [
          el("option", { value: "" }, ["Select session…"])
        ]),
        el("label", { className: "command-option", title: "Shell mode (enables cd, pipes, etc.)" }, [
          el("input", { type: "checkbox", id: "cmd-shell-mode", checked: true }),
          "Shell"
        ]),
        el("span", { id: "cmd-hint", className: "hint" }, [])
      ]),
      el("div", { className: "command-bar-center" }, [
        el("input", {
          id: "cmd-input",
          className: "command-input",
          type: "text",
          placeholder: "Type command and press Enter…",
          autocomplete: "off",
          spellcheck: "false"
        })
      ]),
      el("div", { className: "command-bar-right" }, [
        el("button", { id: "cmd-send-btn", className: "tb-btn", type: "button" }, ["Execute"]),
        el("button", { id: "cmd-history-btn", className: "tb-btn hint", type: "button", title: "History (Up/Down)" }, ["▾"])
      ])
    ]);

    this.root.innerHTML = "";
    this.root.appendChild(header);
    this.root.appendChild(paneWrap);
    this.root.appendChild(commandBar);

    this.paneSet = new PaneSet(paneWrap, {
      maxRows: LIVE_MAX_ROWS,
      bodyClass: "live-body",
    });
    bindPaneWidths(this.paneSet);
    this._nodes = {
      header: header,
      title: title,
      status: status,
      paneWrap: paneWrap,
      commandBar: commandBar
    };

    // 初始化命令栏
    this._initCommandBar();
  };

  // 分栏：新出现的列是空的，要把 buffer 重放一遍才填得满；已经存在的列 DOM 原样
  // 保留，绝不能跟着重放，否则日志会翻倍。records 由调用方（挂钩活跃日志的
  // liveBuffer）提供。
  LiveTail.prototype.setPanes = function (defs, records) {
    var diff = this.paneSet.setPanes(defs);
    if (!diff.added.length) return;

    var filter = {};
    diff.added.forEach(function (id) { filter[id] = true; });
    // count / _runStarts 是跨 run 的累计量，重放只为补 DOM，不能把它们再算一遍
    var savedCount = this.count;
    var savedRunStarts = this._runStarts;
    this._paneFilter = filter;
    this._runStarts = {};
    try {
      (records || []).forEach(this.append, this);
    } finally {
      this._paneFilter = null;
      this.count = savedCount;
      this._runStarts = savedRunStarts;
    }
    this._updateHeader();
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

    // 目标：LiveBody ≈ session 对端 shell 的镜像 + 少量控制流标记。
    //
    // 脚本内 sess.run 与手动 CommandBar 都走同一套显示规则；两者靠 __rid
    // 是否存在区分脉络（有 = 脚本；无 = 手动），视觉打标在 _nodeForRecord。

    // sink 层的 script.begin/end 与 workspace 元行重复，不占屏
    if (r.event === "script.begin" || r.event === "script.end") return;

    // 成功的命令收尾（exit=0）静默——新命令直接接在上一条输出后，像 shell 一样。
    // level=WARNING/ERROR 的（非 0 退出、超时）保留，自带 warn/err 颜色显眼。
    if (r.event === "command_end" && r.level === "INFO") return;

    if (r.event === "workspace.run.begin") {
      if (r.run_id) this._runStarts[r.run_id] = r.started_at || r.timestamp;
      this._appendNode(this._separatorNode(
        "▶ " + (r.script_name || r.run_id || "run") + " · " + (r.run_id || ""),
        "sep-begin"
      ), null);
      this._updateHeader();
      return;
    }
    // workspace.run.end 与 sidebar 的 run 状态徽章重复，不占屏；
    // _runStarts 也用不着了，顺手清一下防止堆积
    if (r.event === "workspace.run.end") {
      if (r.run_id) delete this._runStarts[r.run_id];
      return;
    }

    this.count += 1;
    this._appendNode(this._nodeForRecord(r), r.session_id);
    this._updateHeader();
  };

  LiveTail.prototype._separatorNode = function (text, cls) {
    return el("li", { className: "run-separator " + (cls || "") }, [
      el("span", { className: "run-separator-text" }, [text]),
    ]);
  };

  // LiveBody 专用的简化版节点：隐藏时间戳、事件标签等装饰符，节省空间。
  // 手动 CommandBar 通道（__rid 缺失）打 ev-manual 视觉标，与脚本流区分。
  LiveTail.prototype._nodeForRecord = function (r) {
    var cls = "tl-row " + severityClass(r.level);
    if (!r.__rid) cls += " ev-manual";
    var line = el("li", { className: cls }, []);
    var msg = el("span", { className: "tl-msg" }, [r.message || ""]);
    line.appendChild(msg);
    return line;
  };

  LiveTail.prototype._appendNode = function (node, sessionId) {
    // 重放历史 buffer 时只补新增的列（见 setPanes），老列不能再收一遍
    if (this._paneFilter) {
      var target = this.paneSet.paneFor(sessionId);
      if (!target || !this._paneFilter[target.id]) return;
    }
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

  // ---------- CommandBar （Web UI 手动命令执行） ----------

  var CMD_SESSION_KEY = "rpm.cmd.session";
  var CMD_HISTORY_KEY = "rpm.cmd.history";
  var MAX_LOCAL_HISTORY = 50;

  // 初始化 CommandBar
  LiveTail.prototype._initCommandBar = function () {
    var self = this;
    var cmdInput = document.getElementById("cmd-input");
    var cmdSendBtn = document.getElementById("cmd-send-btn");
    var cmdHistoryBtn = document.getElementById("cmd-history-btn");
    var cmdSessionSelect = document.getElementById("cmd-session-select");

    if (!cmdInput || !cmdSendBtn || !cmdSessionSelect) return;

    // 命令历史状态
    this._cmdHistory = this._loadHistoryFromStorage();
    this._historyIndex = -1;
    this._historyFilter = "";
    this._runningCommands = {};

    // 恢复上次选择的 session
    var lastSession = null;
    try { lastSession = localStorage.getItem(CMD_SESSION_KEY); } catch (e) {}
    if (lastSession) {
      this._pendingSessionSelect = lastSession;
    }

    // 键盘事件
    cmdInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        self._executeCommand();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        self._navigateHistory(-1);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        self._navigateHistory(1);
      } else if (e.key === "Escape") {
        e.preventDefault();
        self._clearInput();
      } else if (e.key === "l" && e.ctrlKey) {
        e.preventDefault();
        self._requestHistoryClear();
      } else {
        // 用户在编辑时重置历史浏览状态
        if (self._historyIndex >= 0) {
          self._historyIndex = -1;
          self._historyFilter = "";
        }
      }
    });

    // 按钮事件
    if (cmdSendBtn) {
      cmdSendBtn.addEventListener("click", function (e) {
        e.preventDefault();
        self._executeCommand();
      });
    }

    if (cmdHistoryBtn) {
      cmdHistoryBtn.addEventListener("click", function (e) {
        e.preventDefault();
        self._toggleHistoryDropdown();
      });
    }

    // Session 选择变化
    cmdSessionSelect.addEventListener("change", function () {
      try { localStorage.setItem(CMD_SESSION_KEY, this.value); } catch (e) {}
      self._updateHistoryForSession(this.value);
    });
  };

  // 更新 Session 选择器
  LiveTail.prototype.updateSessionSelect = function (sessions) {
    var select = document.getElementById("cmd-session-select");
    if (!select) return;

    var currentValue = select.value;
    select.innerHTML = "";

    var firstOption = true;
    Object.keys(sessions).forEach(function (sid) {
      var opt = el("option", { value: sid }, [sid]);
      select.appendChild(opt);
      if (firstOption && !currentValue) {
        currentValue = sid;
        firstOption = false;
      }
    });

    if (currentValue && sessions[currentValue]) {
      select.value = currentValue;
    } else if (select.options.length > 0) {
      select.value = select.options[0].value;
      try { localStorage.setItem(CMD_SESSION_KEY, select.value); } catch (e) {}
    }

    this._updateHistoryForSession(select.value);
  };

  // 执行命令
  LiveTail.prototype._executeCommand = function () {
    var cmdInput = document.getElementById("cmd-input");
    var cmdSessionSelect = document.getElementById("cmd-session-select");
    var cmdShellMode = document.getElementById("cmd-shell-mode");
    var cmdBar = this._nodes.commandBar;

    if (!cmdInput || !cmdSessionSelect) return;

    var command = cmdInput.value.trim();
    var sessionId = cmdSessionSelect.value;

    if (!command) return;
    if (!sessionId) {
      this._showHint("Please select a session first", "warn");
      return;
    }

    // 禁用输入
    cmdInput.disabled = true;
    if (cmdBar) cmdBar.classList.add("running");

    fetch("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        command: command,
        shell: cmdShellMode ? cmdShellMode.checked : true,
        timeout: 30
      })
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.command_id) {
        // 标记为运行中
        if (!window.__cmdRunning) window.__cmdRunning = {};
        window.__cmdRunning[data.command_id] = {
          command: command,
          sessionId: sessionId,
          startTime: Date.now() / 1000
        };
      }
    })
    .catch(function (err) {
      console.error("Command execution failed:", err);
    })
    .finally(function () {
      // 清空输入
      cmdInput.value = "";
      cmdInput.disabled = false;
      if (cmdBar) cmdBar.classList.remove("running");
      cmdInput.focus();
    });
  };

  // 浏览历史
  LiveTail.prototype._navigateHistory = function (direction) {
    var cmdInput = document.getElementById("cmd-input");
    var cmdSessionSelect = document.getElementById("cmd-session-select");
    if (!cmdInput || !cmdSessionSelect) return;

    var sessionId = cmdSessionSelect.value;
    if (!sessionId) return;

    var history = this._cmdHistory[sessionId] || [];
    if (history.length === 0) return;

    // 第一次浏览时保存当前输入
    if (this._historyIndex < 0) {
      this._historyFilter = cmdInput.value;
    }

    var newIndex = this._historyIndex + direction;
    if (newIndex < 0) newIndex = 0;
    if (newIndex >= history.length) {
      // 回到用户正在编辑的内容
      this._historyIndex = -1;
      cmdInput.value = this._historyFilter;
      return;
    }

    this._historyIndex = newIndex;
    cmdInput.value = history[history.length - 1 - newIndex].command;
  };

  // 清空输入
  LiveTail.prototype._clearInput = function () {
    var cmdInput = document.getElementById("cmd-input");
    if (cmdInput) {
      cmdInput.value = "";
      this._historyIndex = -1;
      this._historyFilter = "";
    }
  };

  // 显示提示
  LiveTail.prototype._showHint = function (message, type) {
    var hint = document.getElementById("cmd-hint");
    if (!hint) return;
    hint.textContent = message;
    hint.className = "hint" + (type === "warn" ? " lvl-warn" : "");
    setTimeout(function () {
      if (hint.textContent === message) hint.textContent = "";
    }, 3000);
  };

  // 请求清空历史
  LiveTail.prototype._requestHistoryClear = function () {
    var cmdSessionSelect = document.getElementById("cmd-session-select");
    var sessionId = cmdSessionSelect ? cmdSessionSelect.value : null;

    if (confirm("Clear command history" + (sessionId ? " for " + sessionId : "") + "?")) {
      fetch("/api/commands/history" + (sessionId ? "?session_id=" + encodeURIComponent(sessionId) : ""), {
        method: "DELETE"
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          // 清空本地缓存
          if (sessionId) {
            delete this._cmdHistory[sessionId];
          } else {
            this._cmdHistory = {};
          }
          this._saveHistoryToStorage();
        }
      }.bind(this))
      .catch(function (err) {
        console.error("Failed to clear history:", err);
      });
    }
  };

  // 切换历史下拉菜单
  LiveTail.prototype._toggleHistoryDropdown = function () {
    var existing = document.querySelector(".history-dropdown");
    if (existing) {
      existing.remove();
      return;
    }

    var cmdSessionSelect = document.getElementById("cmd-session-select");
    var sessionId = cmdSessionSelect ? cmdSessionSelect.value : null;
    if (!sessionId) return;

    var history = this._cmdHistory[sessionId] || [];
    if (history.length === 0) {
      this._showHint("No history for this session");
      return;
    }

    var cmdHistoryBtn = document.getElementById("cmd-history-btn");
    if (!cmdHistoryBtn) return;

    var dropdown = el("div", { className: "history-dropdown" }, []);
    history.slice().reverse().forEach(function (item) {
      var div = el("div", { className: "history-item" }, [
        el("span", { className: "cmd-ts" }, [new Date(item.timestamp * 1000).toLocaleTimeString()]),
        el("span", { className: "cmd-text" }, [item.command])
      ]);
      div.addEventListener("click", function () {
        var cmdInput = document.getElementById("cmd-input");
        if (cmdInput) cmdInput.value = item.command;
        dropdown.remove();
      });
      dropdown.appendChild(div);
    });

    var rect = cmdHistoryBtn.getBoundingClientRect();
    dropdown.style.position = "fixed";
    dropdown.style.bottom = (window.innerHeight - rect.top + 4) + "px";
    dropdown.style.right = (window.innerWidth - rect.right) + "px";

    document.body.appendChild(dropdown);

    setTimeout(function () {
      var closeDropdown = function (e) {
        if (!dropdown.contains(e.target) && e.target !== cmdHistoryBtn) {
          dropdown.remove();
          document.removeEventListener("click", closeDropdown);
        }
      };
      setTimeout(function () {
        document.addEventListener("click", closeDropdown);
      }, 0);
    }, 0);
  };

  // 从 localStorage 加载历史
  LiveTail.prototype._loadHistoryFromStorage = function () {
    try {
      var raw = localStorage.getItem(CMD_HISTORY_KEY);
      if (!raw) return {};
      var parsed = JSON.parse(raw);
      return typeof parsed === "object" ? parsed : {};
    } catch (e) {
      return {};
    }
  };

  // 保存历史到 localStorage
  LiveTail.prototype._saveHistoryToStorage = function () {
    try {
      localStorage.setItem(CMD_HISTORY_KEY, JSON.stringify(this._cmdHistory));
    } catch (e) {}
  };

  // 更新当前 session 的历史
  LiveTail.prototype._updateHistoryForSession = function (sessionId) {
    if (!sessionId) return;
    if (!this._cmdHistory[sessionId]) {
      this._cmdHistory[sessionId] = [];
    }
  };

  // 从服务器加载历史
  LiveTail.prototype._loadHistoryFromServer = function (sessionId) {
    var self = this;
    fetch("/api/commands/history?limit=100")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var history = {};
        (data || []).forEach(function (item) {
          var sid = item.session_id;
          if (!history[sid]) history[sid] = [];
          history[sid].push({
            command: item.command,
            timestamp: item.timestamp,
            exit_code: item.exit_code,
            duration: item.duration
          });
        });
        // 合并到现有历史
        for (var sid in history) {
          if (!self._cmdHistory[sid]) self._cmdHistory[sid] = [];
          self._cmdHistory[sid] = history[sid];
        }
        self._saveHistoryToStorage();
      })
      .catch(function (err) {
        console.error("Failed to load history:", err);
      });
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
