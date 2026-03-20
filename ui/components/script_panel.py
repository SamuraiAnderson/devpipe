import streamlit as st

from ui.services.script_service import ScriptService, RunState


def render_script_panel(script_svc: ScriptService):
    st.markdown("#### 脚本列表")

    tree_data = script_svc.scan()
    if not tree_data:
        st.info("未找到脚本")
        return

    _render_nodes(tree_data, script_svc, depth=0)

    if script_svc.state == RunState.RUNNING:
        st.caption(f":arrow_forward: 运行中: `{script_svc.running_id}`")
    elif script_svc.state == RunState.FINISHED:
        st.caption(":white_check_mark: 执行完成")
    elif script_svc.state == RunState.ERROR:
        st.caption(":x: 执行失败")


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

    col_name, col_btn = st.columns([5, 1])
    col_name.markdown(f":page_facing_up: `{node['label']}`")

    if is_this_running:
        if col_btn.button(":stop_button:", key=f"stop_{script_id}", help="停止"):
            script_svc.stop_script()
            st.toast("已发送停止信号")
            st.rerun()
    else:
        if col_btn.button(":arrow_forward:", key=f"run_{script_id}", help="运行"):
            if script_svc.state == RunState.RUNNING:
                st.toast("已有脚本在运行中", icon=":warning:")
            else:
                script_svc.run_script(script_id)
                st.toast(f"开始执行: {node['label']}")
                st.rerun()
