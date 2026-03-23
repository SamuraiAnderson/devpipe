import html
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import streamlit as st

from ui.services.history_service import get_history
from ui.services.log_service import ALL, SCRIPT, close_log_session, get_buffer, get_file_writer
from ui.services.script_analysis import analyze_script


@dataclass
class _TabSource:
    label: str
    log_source: str
    controller_key: Optional[str] = None


_FIXED_TABS = [
    _TabSource(label="全部", log_source=ALL),
    _TabSource(label="脚本", log_source=SCRIPT),
]


def _active_sources() -> list[_TabSource]:
    selected = st.session_state.get("selected_script")
    if not selected:
        return list(_FIXED_TABS)

    controllers = analyze_script(selected)
    if not controllers:
        return list(_FIXED_TABS)

    tabs: list[_TabSource] = list(_FIXED_TABS)
    seen: set[str] = set()
    for c in controllers:
        if c.kind != "controller" or c.log_source in seen:
            continue
        seen.add(c.log_source)
        label = f"{c.platform} ({c.var_name})" if c.var_name else c.platform
        tabs.append(_TabSource(
            label=label,
            log_source=c.log_source,
            controller_key=c.log_source,
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
            _render_log_tab(tab)


@st.fragment(run_every=timedelta(milliseconds=500))
def _render_log_tab(tab: _TabSource):
    records = get_buffer().get_records(tab.log_source)
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

    if tab.controller_key is None:
        return

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
        args=(tab.log_source, tab.controller_key),
    )

    if controller:
        history_cmds = get_history(controller.host).load()
        _inject_history_js(tab.log_source, history_cmds)


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


def _on_cmd_submit(log_source: str, controller_key: str):
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

    try:
        _dispatch_cmd(controller, cmd)
    except Exception as exc:
        controller.log.error("%s", exc)
    st.session_state[widget_key] = ""
