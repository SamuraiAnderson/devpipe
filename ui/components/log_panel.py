import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components

from ui.services.history_service import get_history
from ui.services.log_service import ALL, SCRIPT, close_log_session, get_buffer, get_file_writer
from ui.services.script_analysis import analyze_script, extract_log_filters


@dataclass
class _TabSource:
    label: str
    log_source: str
    controller_key: Optional[str] = None
    include_re: Optional[re.Pattern] = None
    exclude_re: Optional[re.Pattern] = None


_FIXED_TABS = [
    _TabSource(label="全部", log_source=ALL),
    _TabSource(label="脚本", log_source=SCRIPT),
]


_log = logging.getLogger(__name__)


def _compile_re(pattern: Optional[str]) -> Optional[re.Pattern]:
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        _log.warning("无效的日志过滤 regex %r: %s", pattern, exc)
        return None


def _merge_patterns(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """将两条 regex 字符串用 ``|`` 合并；任一为 None 则返回另一条。"""
    if a is None:
        return b
    if b is None:
        return a
    return f"(?:{a})|(?:{b})"


def _resolve_runtime_source(ast_source: str, used: set[str]) -> str:
    """将 AST 预测的 log_source 映射到运行时实际 registry key。

    精确匹配优先；无精确匹配时按平台前缀做唯一候选匹配。
    """
    from src.BaseControl import BaseControl
    registry = BaseControl._registry

    if ast_source in registry:
        used.add(ast_source)
        return ast_source

    platform = ast_source.split('.')[0]
    candidates = [k for k in registry if k.startswith(platform + '.') and k not in used]

    if len(candidates) == 1:
        used.add(candidates[0])
        return candidates[0]

    return ast_source


def _active_sources() -> list[_TabSource]:
    selected = st.session_state.get("selected_script")
    if not selected:
        return list(_FIXED_TABS)

    controllers = analyze_script(selected)
    filters = extract_log_filters(selected)
    wildcard = filters.pop("*", None)

    var_to_source: dict[str, str] = {}
    for c in controllers:
        if c.var_name:
            var_to_source[c.var_name] = c.log_source

    resolved: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for key, rule in filters.items():
        log_source = var_to_source.get(key, key)
        resolved[log_source] = (rule.include, rule.exclude)

    def _build_tab(log_source: str, ast_source: str = None, **kwargs) -> _TabSource:
        inc, exc = resolved.get(log_source, (None, None))
        if (inc, exc) == (None, None) and ast_source:
            inc, exc = resolved.get(ast_source, (None, None))
        if wildcard:
            inc = _merge_patterns(inc, wildcard.include)
            exc = _merge_patterns(exc, wildcard.exclude)
        return _TabSource(
            log_source=log_source,
            include_re=_compile_re(inc),
            exclude_re=_compile_re(exc),
            **kwargs,
        )

    tabs: list[_TabSource] = [
        _build_tab(ALL, label="全部"),
        _build_tab(SCRIPT, label="脚本"),
    ]

    if not controllers:
        return tabs

    seen: set[str] = set()
    used: set[str] = set()
    for c in controllers:
        if c.kind != "controller" or c.log_source in seen:
            continue
        seen.add(c.log_source)
        runtime_source = _resolve_runtime_source(c.log_source, used)
        label = f"{c.platform} ({c.var_name})" if c.var_name else c.platform
        tabs.append(_build_tab(
            runtime_source,
            ast_source=c.log_source,
            label=label,
            controller_key=runtime_source,
        ))
    return tabs


def render_log_panel():
    tabs = _active_sources()

    col_tabs, col_clear = st.columns([9, 1])
    with col_tabs:
        tab_objects = st.tabs([t.label for t in tabs])
    with col_clear:
        st.markdown("")
        if st.button(":wastebasket:", key="clear_all", help="清空日志", use_container_width=True):
            get_buffer().clear_all()
            close_log_session()

    for tab_obj, tab in zip(tab_objects, tabs):
        with tab_obj:
            if tab.controller_key is not None:
                _render_controller_tab(tab)
            elif tab.log_source == SCRIPT:
                _render_script_tab(tab)
            else:
                _render_log_view(tab)


# ── 控制器 Tab：模式切换 + 日志/终端 + 输入框 ──

def _render_controller_tab(tab: _TabSource):
    col_mode, _ = st.columns([1, 5])
    with col_mode:
        terminal_mode = st.toggle("终端", key=f"term_mode_{tab.log_source}")

    if terminal_mode:
        _render_xterm_iframe(tab)
    else:
        _render_log_view(tab)

    _render_cmd_input(tab, terminal_mode)


def _render_xterm_iframe(tab: _TabSource):
    session_key = tab.controller_key
    components.iframe(
        f"http://localhost:8766/terminal/{session_key}",
        height=500,
        scrolling=False,
    )


def _render_cmd_input(tab: _TabSource, terminal_mode: bool):
    """底部命令输入框，两种模式均可用。"""
    client_svc = st.session_state.get("client_svc")
    controller = (
        client_svc.get_controller_by_key(tab.controller_key)
        if client_svc else None
    )
    disabled = controller is None

    if controller:
        placeholder = f"{controller.host}:{controller.pwd}$ "
    else:
        placeholder = "未连接"

    st.text_input(
        "命令",
        key=f"cmd_input_{tab.log_source}",
        placeholder=placeholder,
        disabled=disabled,
        label_visibility="collapsed",
        on_change=_on_cmd_submit,
        args=(tab.log_source, tab.controller_key, terminal_mode),
    )

    if controller:
        history_cmds = get_history(controller.host).load()
        _inject_history_js(tab.log_source, history_cmds)


# ── 脚本 Tab：日志 + stdin 输入框 ──

def _render_script_tab(tab: _TabSource):
    _render_log_view(tab)

    script_svc = st.session_state.get("script_svc")
    if script_svc and script_svc.is_running():
        waiting = script_svc.waiting_for_input
        placeholder = "脚本等待输入..." if waiting else "脚本运行中（无输入请求）"
        st.text_input(
            "脚本输入",
            key="script_stdin_input",
            placeholder=placeholder,
            disabled=not waiting,
            label_visibility="collapsed",
            on_change=_on_script_stdin_submit,
        )


# ── 日志视图（fragment，500ms 轮询）──

def _apply_filters(records, tab: _TabSource):
    """根据 include_re / exclude_re 过滤日志记录列表。"""
    if tab.include_re is None and tab.exclude_re is None:
        return records
    out = records
    if tab.include_re is not None:
        out = [r for r in out if tab.include_re.search(r.message)]
    if tab.exclude_re is not None:
        out = [r for r in out if not tab.exclude_re.search(r.message)]
    return out


@st.fragment(run_every=timedelta(milliseconds=500))
def _render_log_view(tab: _TabSource):
    records = _apply_filters(get_buffer().get_records(tab.log_source), tab)
    fmt = (lambda r: r.formatted_all()) if tab.log_source == ALL else (lambda r: r.formatted())
    lines = "<br>".join(html.escape(fmt(r)) for r in records) if records else "(空)"
    uid = f"log-{tab.log_source}"
    terminal_html = (
        f'<div class="log-terminal" id="{uid}">'
        f"{lines}"
        f"</div>"
        f'<script>var e=document.getElementById("{uid}");'
        f"e.scrollTop=e.scrollHeight;</script>"
    )
    st.markdown(terminal_html, unsafe_allow_html=True)


# ── 命令历史 JS 注入 ──

def _inject_history_js(source: str, history: list[str]):
    """注入 JavaScript，为当前 Tab 的输入框添加上下键切换命令历史。"""
    history_json = json.dumps(history, ensure_ascii=False).replace("</", r"<\/")
    marker_id = f"hist-{source}"
    script = (
        f'<div id="{marker_id}" style="display:none"></div>'
        "<script>"
        "setTimeout(function(){"
        f'var m=document.getElementById("{marker_id}");'
        "if(!m)return;"
        f"var h={history_json};"
        'var c=m;while(c&&!c.querySelector(\'input[type="text"]\')){c=c.parentElement;}'
        "if(!c)return;"
        'var inp=c.querySelector(\'input[type="text"]\');'
        "if(!inp)return;"
        "var ol=inp._cmdHistLen||0;"
        "inp._cmdHistory=h;inp._cmdHistLen=h.length;"
        "if(h.length!==ol){inp._histIdx=h.length;}"
        "if(inp._hBound)return;"
        "inp._hBound=true;"
        "var set=Object.getOwnPropertyDescriptor("
        "window.HTMLInputElement.prototype,'value').set;"
        "inp.addEventListener('keydown',function(e){"
        "var hist=inp._cmdHistory;"
        "if(!hist||!hist.length)return;"
        "if(e.key==='ArrowUp'){"
        "e.preventDefault();"
        "if(inp._histIdx>0)inp._histIdx--;"
        "set.call(inp,hist[inp._histIdx]||'');"
        "inp.dispatchEvent(new Event('input',{bubbles:true}));"
        "}else if(e.key==='ArrowDown'){"
        "e.preventDefault();"
        "if(inp._histIdx<hist.length-1){"
        "inp._histIdx++;"
        "set.call(inp,hist[inp._histIdx]);"
        "}else{"
        "inp._histIdx=hist.length;"
        "set.call(inp,'');"
        "}"
        "inp.dispatchEvent(new Event('input',{bubbles:true}));"
        "}"
        "});"
        "},0);"
        "</script>"
    )
    st.markdown(script, unsafe_allow_html=True)


# ── 命令提交回调 ──

def _dispatch_cmd(controller, cmd: str):
    """拦截用户命令，处理内建命令，转发其余命令到 shell。"""
    user = getattr(controller, 'user', '')
    prefix = f"{user}@{controller.host}" if user else controller.host
    controller.log.info(f"{prefix}:{controller.pwd}$ {cmd}")

    stripped = cmd.strip()

    m = re.match(r'^cd(?:\s+(.+))?$', stripped)
    if m and not re.search(r'[;&|]', stripped):
        target = m.group(1) or '~'
        controller.cd(target)
        controller.log.info(controller.pwd)
        return

    controller.shell(cmd)


def _on_cmd_submit(log_source: str, controller_key: str, terminal_mode: bool = False):
    widget_key = f"cmd_input_{log_source}"
    cmd = st.session_state.get(widget_key, "").strip()
    if not cmd:
        return

    client_svc = st.session_state.get("client_svc")
    controller = (
        client_svc.get_controller_by_key(controller_key)
        if client_svc else None
    )
    if controller is None:
        return

    get_history(controller.host).add(cmd)

    if terminal_mode:
        from ui.services.terminal_service import get_session
        session = get_session(controller_key)
        if session and not session.closed:
            session.write((cmd + '\n').encode())
    else:
        try:
            _dispatch_cmd(controller, cmd)
        except Exception as exc:
            controller.log.error("%s", exc)

    st.session_state[widget_key] = ""


def _on_script_stdin_submit():
    cmd = st.session_state.get("script_stdin_input", "").strip()
    if not cmd:
        return
    script_svc = st.session_state.get("script_svc")
    if script_svc and hasattr(script_svc, 'provide_input'):
        script_svc.provide_input(cmd)
    st.session_state["script_stdin_input"] = ""
