import html
import json
import re
from datetime import timedelta

import streamlit as st

from ui.services.client_service import Platform
from ui.services.history_service import get_history
from ui.services.log_service import ALL, SCRIPT, SOURCES, get_buffer, get_file_writer
from ui.services.script_analysis import analyze_script

_SOURCE_LABELS = {ALL: '全部', SCRIPT: '脚本', 'Local': 'Local', 'Linux': 'Linux', 'Android': 'Android', 'Serial': 'Serial'}

_SOURCE_TO_PLATFORM = {
    'Local': Platform.LOCAL,
    'Linux': Platform.LINUX,
    'Android': Platform.ANDROID,
}


def _active_sources() -> list[str]:
    selected = st.session_state.get("selected_script")
    if not selected:
        return list(SOURCES)

    controllers = analyze_script(selected)
    if not controllers:
        return list(SOURCES)

    platforms = list(dict.fromkeys(
        c.platform for c in controllers if c.kind == "controller"
    ))
    return [ALL, SCRIPT] + platforms


def render_log_panel():
    sources = _active_sources()

    col_tabs, col_clear = st.columns([9, 1])
    with col_tabs:
        tab_objects = st.tabs([_SOURCE_LABELS.get(s, s) for s in sources])
    with col_clear:
        st.markdown("")
        if st.button(":wastebasket:", key="clear_all", help="清空日志", use_container_width=True):
            for source in sources:
                get_buffer().clear(source)
                get_file_writer().rotate(source)

    for tab, source in zip(tab_objects, sources):
        with tab:
            _render_log_tab(source)


@st.fragment(run_every=timedelta(milliseconds=500))
def _render_log_tab(source: str):
    records = get_buffer().get_records(source)
    fmt = (lambda r: r.formatted_all()) if source == ALL else (lambda r: r.formatted())
    lines = "<br>".join(html.escape(fmt(r)) for r in records) if records else "(空)"
    uid = f"log-{source}"
    terminal_html = (
        f'<div class="log-terminal" id="{uid}">'
        f"{lines}"
        f"</div>"
        f'<script>var e=document.getElementById("{uid}");'
        f"e.scrollTop=e.scrollHeight;</script>"
    )
    st.markdown(terminal_html, unsafe_allow_html=True)

    platform = _SOURCE_TO_PLATFORM.get(source)
    if platform is None:
        return

    client_svc = st.session_state.get("client_svc")
    controller = client_svc.get_controller(platform) if client_svc else None
    disabled = controller is None

    if controller:
        placeholder = f"{controller.host}:{controller.pwd}$ "
    else:
        placeholder = "未连接"

    st.text_input(
        "命令",
        key=f"cmd_input_{source}",
        placeholder=placeholder,
        disabled=disabled,
        label_visibility="collapsed",
        on_change=_on_cmd_submit,
        args=(source,),
    )

    if controller:
        history_cmds = get_history(controller.host).load()
        _inject_history_js(source, history_cmds)


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


def _on_cmd_submit(source: str):
    key = f"cmd_input_{source}"
    cmd = st.session_state.get(key, "").strip()
    if not cmd:
        return
    platform = _SOURCE_TO_PLATFORM.get(source)
    if platform is None:
        return
    client_svc = st.session_state.get("client_svc")
    controller = client_svc.get_controller(platform) if client_svc else None
    if controller is None:
        return

    get_history(controller.host).add(cmd)

    try:
        _dispatch_cmd(controller, cmd)
    except Exception as exc:
        controller.log.error("%s", exc)
    st.session_state[key] = ""
