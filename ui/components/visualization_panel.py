"""Visualization panel — card-style display of controllers detected in a script."""

import streamlit as st

from ui.services.script_analysis import ControllerInfo, analyze_script

_PLATFORM_ICONS = {
    "Local":   ":computer:",
    "Linux":   ":desktop_computer:",
    "Android": ":iphone:",
    "Serial":  ":electric_plug:",
}

_PLATFORM_COLORS = {
    "Local":   "blue",
    "Linux":   "green",
    "Android": "orange",
    "Serial":  "violet",
}


def _render_controller_card(info: ControllerInfo):
    icon = _PLATFORM_ICONS.get(info.platform, ":gear:")
    color = _PLATFORM_COLORS.get(info.platform, "gray")

    var_label = f"  `{info.var_name}`" if info.var_name else ""
    st.markdown(f"### {icon} {info.platform}{var_label}")

    st.caption(f"类: `{info.class_name}`")

    if info.params:
        for key, value in info.params.items():
            st.markdown(f"- **{key}**: `{value}`")
    else:
        st.markdown("_无构造参数_")


def render_visualization_panel():
    selected = st.session_state.get("selected_script")

    if not selected:
        st.info("在左侧脚本列表中点击 :material/visibility: 选择一个脚本以查看其终端信息")
        return

    st.subheader(f":page_facing_up: {selected}", divider="gray")

    controllers = analyze_script(selected)

    if not controllers:
        st.warning("未检测到控制器实例化（AdbCnet / Linux / LocalHost / SerialControl）")
        return

    cols = st.columns(min(len(controllers), 3))
    for idx, info in enumerate(controllers):
        with cols[idx % len(cols)]:
            with st.container(border=True):
                _render_controller_card(info)
