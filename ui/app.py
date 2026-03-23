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

    from ui.services.terminal_service import ensure_ws_server
    ensure_ws_server(port=8766)

from ui.components.log_panel import render_log_panel
from ui.components.script_panel import render_script_panel
from ui.components.visualization_panel import render_visualization_panel

st.markdown(
    """
    <style>
    .block-container { padding-top: 1rem !important; padding-bottom: 0.5rem !important; }
    [data-testid="stSidebar"] > div:first-child { padding-top: 1rem; }
    [data-testid="stSidebar"] { min-width: 300px; }
    [data-testid="stSidebar"] button {
        padding: 0.25rem 0.4rem;
        min-height: 2rem;
    }
    [data-testid="stSidebar"] [data-testid="column"] p {
        line-height: 2rem;
    }
    .log-terminal {
        background-color: #012456;
        color: #cccccc;
        font-family: 'Cascadia Code', 'JetBrains Mono', Consolas, 'Courier New', monospace;
        font-size: 13px;
        line-height: 1.4;
        height: calc(100vh - 22rem);
        min-height: 200px;
        overflow-y: auto;
        border-radius: 6px;
        padding: 0.6rem 0.8rem;
    }
    .log-terminal {
        word-wrap: break-word;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### RedPyMake")
    render_script_panel(st.session_state.script_svc)

render_visualization_panel()
render_log_panel()
