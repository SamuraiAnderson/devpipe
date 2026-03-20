from datetime import timedelta

import streamlit as st

from ui.services.log_service import ALL, SOURCES, get_buffer, get_file_writer

_TAB_LABELS = {ALL: '全部', 'Local': 'Local', 'Linux': 'Linux', 'Android': 'Android'}


@st.fragment(run_every=timedelta(milliseconds=100))
def render_log_panel():
    tab_objects = st.tabs([_TAB_LABELS[s] for s in SOURCES])

    for tab, source in zip(tab_objects, SOURCES):
        with tab:
            records = get_buffer().get_records(source)
            log_text = "\n".join(r.formatted() for r in records)
            st.code(log_text or "(空)", language="log", line_numbers=False)

    col1, col2 = st.columns(2)
    if col1.button(":wastebasket: 清空当前", key="clear_cur"):
        current_idx = st.session_state.get("log_tab_idx", 0)
        source = SOURCES[current_idx] if current_idx < len(SOURCES) else ALL
        get_buffer().clear(source)
        get_file_writer().rotate(source)
    if col2.button(":wastebasket: 清空全部", key="clear_all"):
        get_buffer().clear_all()
        get_file_writer().rotate_all()
