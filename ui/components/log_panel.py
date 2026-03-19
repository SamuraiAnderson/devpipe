from nicegui import ui

from ui.services.log_service import ALL, LogRecord, get_handler, install

_LOG_STYLE = (
    'background: #f8f9fa;'
    'color: #2c3e50;'
    'border: none;'
    'border-top: 1px solid #e4e8ed;'
    'padding: 8px 12px;'
    'font-family: "JetBrains Mono", Consolas, "Courier New", monospace;'
)

_SOURCES = [ALL, 'Local', 'Linux', 'Android']
_TAB_LABELS = {ALL: '全部', 'Local': 'Local', 'Linux': 'Linux', 'Android': 'Android'}


class LogPanel:
    def __init__(self):
        self._logs: dict[str, ui.log] = {}
        self._build()
        install(level=0)
        handler = get_handler()
        for source in _SOURCES:
            handler.register(source, lambda r, s=source: self._on_record(s, r))

    def _build(self):
        with ui.column().classes('w-full h-full gap-0'):
            with ui.row().classes('w-full items-center no-wrap px-2').style(
                'border-bottom: 1px solid #e4e8ed;'
            ):
                with ui.tabs().classes('flex-grow') as self._tabs:
                    for source in _SOURCES:
                        ui.tab(source, label=_TAB_LABELS[source])
                ui.button(
                    icon='delete_outline',
                    on_click=self._clear_all,
                ).props('flat dense round size=sm color=grey-6')

            with ui.tab_panels(self._tabs, value=ALL).classes('w-full flex-grow q-pa-none'):
                for source in _SOURCES:
                    with ui.tab_panel(source).classes('q-pa-none h-full'):
                        self._logs[source] = (
                            ui.log(max_lines=1000)
                            .classes('w-full h-full font-mono text-xs leading-relaxed')
                            .style(_LOG_STYLE)
                        )

    def _on_record(self, source: str, record: LogRecord):
        widget = self._logs.get(source)
        if widget is not None:
            widget.push(record.formatted())

    def _clear_all(self):
        for widget in self._logs.values():
            widget.clear()
