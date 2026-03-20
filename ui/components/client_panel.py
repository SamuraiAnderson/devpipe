import streamlit as st

from ui.services.client_service import ClientService, ConnState, Platform

_PLATFORM_ICONS = {
    Platform.LOCAL:   ":computer:",
    Platform.LINUX:   ":desktop_computer:",
    Platform.ANDROID: ":iphone:",
}

_STATE_COLOR = {
    ConnState.CONNECTED:    "green",
    ConnState.DISCONNECTED: "gray",
    ConnState.ERROR:        "red",
}

_STATE_LABEL = {
    ConnState.CONNECTED:    "已连接",
    ConnState.DISCONNECTED: "未连接",
    ConnState.ERROR:        "连接失败",
}


def render_client_panel(client_svc: ClientService):
    st.subheader("客户端", divider="gray")

    for platform in (Platform.LOCAL, Platform.LINUX, Platform.ANDROID):
        info = client_svc.clients[platform]
        color = _STATE_COLOR[info.state]

        cols = st.columns([1, 2, 3, 3, 2])
        cols[0].markdown(_PLATFORM_ICONS[platform])
        cols[1].markdown(f"**{platform.value}**")
        cols[2].caption(info.host or "-")
        cols[3].markdown(f":{color}_circle: {_STATE_LABEL[info.state]}")

        if platform != Platform.LOCAL:
            if info.state == ConnState.CONNECTED:
                if cols[4].button("断开", key=f"disc_{platform.value}", type="secondary"):
                    client_svc.disconnect(platform)
                    st.rerun()
            else:
                if cols[4].button("连接", key=f"conn_{platform.value}", type="primary"):
                    client_svc.connect(platform)
                    st.rerun()
