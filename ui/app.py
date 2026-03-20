"""RedPyMake UI — Streamlit entry point."""

import logging
import sys
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ui.services.client_service import ClientService
from ui.services.script_service import ScriptService
from ui.services.log_service import install

st.set_page_config(page_title="RedPyMake", layout="wide", page_icon=":wrench:")

if "client_svc" not in st.session_state:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s", force=True)
    install(level=0)
    st.session_state.client_svc = ClientService()
    st.session_state.script_svc = ScriptService()

from ui.components.client_panel import render_client_panel
from ui.components.log_panel import render_log_panel
from ui.components.script_panel import render_script_panel

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { min-width: 280px; }
    .log-container pre { font-family: 'JetBrains Mono', Consolas, 'Courier New', monospace; font-size: 12px; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### RedPyMake")
    render_script_panel(st.session_state.script_svc)

render_client_panel(st.session_state.client_svc)
st.divider()
render_log_panel()
