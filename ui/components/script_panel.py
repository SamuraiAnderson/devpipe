import streamlit as st

from ui.services.script_service import ScriptService, RunState

_STATUS_CONFIG = {
    RunState.RUNNING:  (":material/play_circle:",  "运行中"),
    RunState.FINISHED: (":material/check_circle:",  "执行完成"),
    RunState.ERROR:    (":material/error:",          "执行失败"),
}


def render_script_panel(script_svc: ScriptService):
    st.markdown("#### 脚本列表")

    tree_data = script_svc.scan()
    if not tree_data:
        st.info("未找到脚本")
        return

    _render_nodes(tree_data, script_svc, depth=0)

    cfg = _STATUS_CONFIG.get(script_svc.state)
    if cfg:
        icon, text = cfg
        label = f"{icon} {text}: `{script_svc.running_id}`" if script_svc.state == RunState.RUNNING else f"{icon} {text}"
        st.caption(label)


def _render_nodes(nodes: list[dict], script_svc: ScriptService, depth: int):
    for node in nodes:
        children = node.get('children')
        if children:
            with st.expander(f":file_folder: {node['label']}", expanded=(depth == 0)):
                _render_nodes(children, script_svc, depth + 1)
        else:
            _render_script_file(node, script_svc)


def _render_script_file(node: dict, script_svc: ScriptService):
    script_id = node['id']
    is_this_running = (
        script_svc.state == RunState.RUNNING and script_svc.running_id == script_id
    )
    is_selected = st.session_state.get("selected_script") == script_id

    col_name, col_view, col_run = st.columns([4, 1.2, 1.2])

    if is_selected:
        col_name.markdown(f":material/description: **{node['label']}**")
    else:
        col_name.markdown(f":material/draft: {node['label']}")

    col_view.button(
        ":material/visibility:" if not is_selected else ":material/check:",
        key=f"sel_{script_id}",
        help="查看脚本详情",
        type="primary" if is_selected else "secondary",
        use_container_width=True,
        on_click=_on_select,
        args=(script_id,),
    )

    if is_this_running:
        col_run.button(
            ":material/stop_circle:",
            key=f"stop_{script_id}",
            help="停止运行",
            type="primary",
            use_container_width=True,
            on_click=_on_stop,
            args=(script_svc,),
        )
    else:
        disabled = script_svc.state == RunState.RUNNING
        col_run.button(
            ":material/play_arrow:",
            key=f"run_{script_id}",
            help="已有脚本运行中" if disabled else "运行脚本",
            disabled=disabled,
            use_container_width=True,
            on_click=_on_run,
            args=(script_svc, script_id, node['label']),
        )


def _on_select(script_id: str):
    st.session_state.selected_script = script_id


def _on_stop(script_svc: ScriptService):
    script_svc.stop_script()
    st.toast("已发送停止信号", icon=":material/stop_circle:")


def _on_run(script_svc: ScriptService, script_id: str, label: str):
    if script_svc.state == RunState.RUNNING:
        st.toast("已有脚本在运行中", icon=":material/warning:")
        return
    script_svc.run_script(script_id)
    st.toast(f"开始执行: {label}", icon=":material/play_arrow:")
