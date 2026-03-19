"""RedPyMake UI — entry point."""

import logging

from nicegui import ui

from ui.services.client_service import ClientService
from ui.services.script_service import ScriptService

THEME_CSS = """
:root {
    --bg-base:    #fafbfc;
    --bg-surface: #ffffff;
    --bg-mantle:  #f0f2f5;
    --fg:         #2c3e50;
    --fg-dim:     #7f8c9b;
    --border:     #e4e8ed;
    --accent:     #4a90d9;
}
body, .q-page, .nicegui-content {
    background: var(--bg-base) !important;
    color: var(--fg) !important;
}
.q-header {
    background: var(--bg-surface) !important;
    box-shadow: none !important;
    border-bottom: 1px solid var(--border);
}
.q-splitter__separator {
    background: var(--border) !important;
}
.q-tree__node-header {
    color: var(--fg) !important;
    border-radius: 4px;
}
.q-tree__node-header:hover {
    background: rgba(74, 144, 217, 0.06) !important;
}
.q-separator {
    background: var(--border) !important;
}
.q-card {
    box-shadow: none !important;
}
"""

client_svc = ClientService()
script_svc = ScriptService()


@ui.page('/')
def index():
    from ui.components.client_panel import ClientPanel
    from ui.components.log_panel import LogPanel
    from ui.components.script_panel import ScriptPanel

    ui.add_head_html(f'<style>{THEME_CSS}</style>')
    ui.add_head_html(
        '<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">'
    )

    with ui.header().classes('items-center px-4 py-2'):
        ui.label('RedPyMake').classes('text-h6 font-bold').style('color: #2c3e50; letter-spacing: 1px;')
        ui.space()
        connected = sum(
            1 for c in client_svc.clients.values()
            if c.state.value == 'connected'
        )
        total = len(client_svc.clients)
        ui.label(f'已连接 {connected}/{total} 客户端').classes('text-caption').style('color: #7f8c9b;')

    with ui.splitter(value=25).classes('w-full').style('height: calc(100vh - 52px);') as splitter:
        with splitter.before:
            with ui.column().classes('w-full h-full').style(
                'background: var(--bg-surface); border-right: 1px solid var(--border);'
            ):
                ScriptPanel(script_svc)

        with splitter.after:
            with ui.column().classes('w-full h-full gap-0'):
                ClientPanel(client_svc)
                ui.separator()
                with ui.column().classes('w-full flex-grow gap-0').style('min-height: 0;'):
                    LogPanel()


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s: %(message)s",
        force=True,
    )
    ui.run(
        title='RedPyMake',
        port=8080,
        reload=False,
        dark=False,
    )


if __name__ in {'__main__', '__mp_main__'}:
    main()
