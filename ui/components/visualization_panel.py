"""Visualization panel — card-style display of controllers detected in a script."""

import streamlit as st

from ui.services.script_analysis import ControllerInfo, analyze_script

_PLATFORM_ICONS = {
    "Local":   ":computer:",
    "Linux":   ":desktop_computer:",
    "Android": ":iphone:",
    "Serial":  ":electric_plug:",
    "TFTP":    ":open_file_folder:",
}


def _render_controller_card(info: ControllerInfo):
    icon = _PLATFORM_ICONS.get(info.platform, ":gear:")

    var_part = f" (`{info.var_name}`)" if info.var_name else ""
    st.markdown(f"**{icon} {info.platform}** · `{info.class_name}`{var_part}")

    if info.params:
        params_str = " &nbsp; ".join(f"`{k}={v}`" for k, v in info.params.items())
        st.caption(params_str)
    else:
        st.caption("_无构造参数_")


def render_visualization_panel():
    selected = st.session_state.get("selected_script")

    if not selected:
        st.info("在左侧脚本列表中点击 :material/visibility: 选择一个脚本以查看其终端信息")
        return

    st.subheader(f":page_facing_up: {selected}", divider="gray")

    controllers = analyze_script(selected)

    if not controllers:
        st.warning("未检测到控制器或服务实例化（AdbCnet / Linux / LocalHost / SerialControl / TftpdServer）")
        return

    cols = st.columns(min(len(controllers), 3))
    for idx, info in enumerate(controllers):
        with cols[idx % len(cols)]:
            with st.container(border=True, key=f"viz_{info.key}"):
                _render_controller_card(info)
