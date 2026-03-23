"""Tornado WebSocket 服务：xterm.js 终端页面 + 双向数据中继 + session 管理。"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Optional

import tornado.ioloop
import tornado.web
import tornado.websocket

log = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

# ── session 管理 ──

_sessions: dict[str, 'InteractiveSession'] = {}
_lock = threading.Lock()


def _get_or_create_session(key: str):
    from src.BaseControl import BaseControl

    with _lock:
        session = _sessions.get(key)
        if session is not None and not session.closed:
            return session
        ctrl = BaseControl._registry.get(key)
        if ctrl is None:
            return None
        session = ctrl.open_interactive()
        _sessions[key] = session
        log.info("交互会话已创建: %s", key)
        return session


def get_session(key: str):
    """供 log_panel 输入框回调直接调用。"""
    with _lock:
        session = _sessions.get(key)
        if session is not None and not session.closed:
            return session
    return None


def close_session(key: str):
    with _lock:
        session = _sessions.pop(key, None)
    if session and not session.closed:
        session.close()
        log.info("交互会话已关闭: %s", key)


# ── xterm.js 终端 HTML 模板 ──

_XTERM_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"/>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5/css/xterm.css"/>
<script src="https://cdn.jsdelivr.net/npm/xterm@5/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10/lib/addon-fit.js"></script>
<style>body {{ margin:0; overflow:hidden; background:#012456; }} #terminal {{ height:100vh; }}</style>
</head><body>
<div id="terminal"></div>
<script>
  var term = new Terminal({{
    cursorBlink: true, fontSize: 13,
    fontFamily: "'Cascadia Code','JetBrains Mono',Consolas,monospace",
    theme: {{ background: '#012456' }}
  }});
  var fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById('terminal'));
  fitAddon.fit();

  var wsUrl = 'ws://' + location.host + '/ws/terminal/{session_key}';
  var ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = function() {{ term.focus(); }};
  ws.onmessage = function(e) {{ term.write(new Uint8Array(e.data)); }};
  ws.onclose = function() {{ term.write('\\r\\n[连接已断开]\\r\\n'); }};
  term.onData(function(d) {{ if (ws.readyState === 1) ws.send(d); }});
  term.onResize(function(size) {{
    if (ws.readyState === 1)
      ws.send(JSON.stringify({{type:'resize', cols:size.cols, rows:size.rows}}));
  }});
  window.addEventListener('resize', function() {{ fitAddon.fit(); }});
</script>
</body></html>"""


# ── Tornado handlers ──

class TerminalPageHandler(tornado.web.RequestHandler):
    """Serve xterm.js 终端页面，由 Streamlit iframe 嵌入。"""

    def get(self, session_key: str):
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.write(_XTERM_HTML.format(session_key=session_key))


class TerminalWebSocketHandler(tornado.websocket.WebSocketHandler):
    """双向数据中继：xterm.js ↔ InteractiveSession。"""

    session: Optional[object]
    _polling: bool
    _session_key: str

    def check_origin(self, origin):
        return True

    def open(self, session_key: str):
        self._session_key = session_key
        self.session = _get_or_create_session(session_key)
        if self.session is None:
            self.write_message(b'[controller not found]\r\n', binary=True)
            self.close()
            return
        self._polling = True
        self._schedule_read()
        log.info("WebSocket 已连接: %s", session_key)

    def _schedule_read(self):
        if self._polling:
            tornado.ioloop.IOLoop.current().call_later(0.02, self._poll)

    def _poll(self):
        if not self._polling or self.ws_connection is None:
            return
        if self.session is None or self.session.closed:
            self._polling = False
            try:
                self.write_message(b'\r\n[session closed]\r\n', binary=True)
            except Exception:
                pass
            return
        data = self.session.read_nonblocking()
        if data:
            try:
                self.write_message(data, binary=True)
            except Exception:
                self._polling = False
                return
            self._tee_to_log(data)
        self._schedule_read()

    def _tee_to_log(self, data: bytes):
        """将终端输出写入 LogBuffer（剥离 ANSI 转义）。"""
        from src.BaseControl import BaseControl

        ctrl = BaseControl._registry.get(self._session_key)
        if ctrl is None:
            return
        text = data.decode(errors='replace')
        clean = _ANSI_ESCAPE.sub('', text)
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped:
                ctrl.log.info(stripped)

    def on_message(self, message):
        if self.session is None or self.session.closed:
            return
        if isinstance(message, str):
            try:
                import json
                msg = json.loads(message)
                if isinstance(msg, dict) and msg.get('type') == 'resize':
                    self.session.resize(msg.get('cols', 120), msg.get('rows', 40))
                    return
            except (json.JSONDecodeError, ValueError):
                pass
            message = message.encode()
        self.session.write(message)

    def on_close(self):
        self._polling = False
        log.info("WebSocket 已断开: %s", getattr(self, '_session_key', '?'))


# ── 服务启动 ──

_server_started = False
_server_lock = threading.Lock()


def ensure_ws_server(port: int = 8766):
    """幂等启动 Tornado WebSocket 服务（后台线程 + 独立事件循环）。"""
    global _server_started
    with _server_lock:
        if _server_started:
            return
        _server_started = True

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = tornado.web.Application([
            (r'/terminal/(.*)', TerminalPageHandler),
            (r'/ws/terminal/(.*)', TerminalWebSocketHandler),
        ])
        app.listen(port)
        log.info("终端 WebSocket 服务已启动 (port=%d)", port)
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True, name='terminal-ws')
    t.start()
