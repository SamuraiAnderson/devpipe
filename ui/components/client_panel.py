from nicegui import ui

from ui.services.client_service import ClientService, ConnState, Platform

_PLATFORM_ICONS = {
    Platform.LOCAL:   'computer',
    Platform.LINUX:   'dns',
    Platform.ANDROID: 'phone_android',
}

_STATE_COLORS = {
    ConnState.CONNECTED:    '#22c55e',
    ConnState.DISCONNECTED: '#9ca3af',
    ConnState.ERROR:        '#ef4444',
}

_STATE_LABELS = {
    ConnState.CONNECTED:    '已连接',
    ConnState.DISCONNECTED: '未连接',
    ConnState.ERROR:        '连接失败',
}


class ClientPanel:
    def __init__(self, client_service: ClientService):
        self._svc = client_service
        self._rows: dict[Platform, _RowWidgets] = {}
        self._build()

    def _build(self):
        with ui.column().classes('w-full gap-0 py-1'):
            for platform in (Platform.LOCAL, Platform.LINUX, Platform.ANDROID):
                self._build_row(platform)

    def _build_row(self, platform: Platform):
        info = self._svc.clients[platform]
        color = _STATE_COLORS[info.state]

        with ui.row().classes('w-full items-center px-4 py-1 gap-3 no-wrap').style(
            'min-height: 28px;'
        ):
            ui.icon(_PLATFORM_ICONS[platform]).classes('text-sm').style('color: #4a90d9;')
            ui.label(platform.value).classes('text-sm').style('min-width: 60px; font-weight: 500;')
            host_label = ui.label(info.host or '-').classes('text-xs').style(
                'color: #7f8c9b; min-width: 120px;'
            )
            dot = ui.icon('circle').style(f'color: {color}; font-size: 8px;')
            state_label = ui.label(_STATE_LABELS[info.state]).classes('text-xs').style(
                f'color: {color};'
            )

            if platform != Platform.LOCAL:
                btn = ui.button(
                    '断开' if info.state == ConnState.CONNECTED else '连接',
                    on_click=lambda _, p=platform: self._toggle(p),
                ).props('flat dense size=xs').style('color: #4a90d9; font-size: 11px;')
            else:
                btn = None

        self._rows[platform] = _RowWidgets(dot, state_label, host_label, btn)

    def _toggle(self, platform: Platform):
        info = self._svc.clients[platform]
        if info.state == ConnState.CONNECTED:
            self._svc.disconnect(platform)
        else:
            self._svc.connect(platform)
        self._refresh(platform)

    def _refresh(self, platform: Platform):
        info = self._svc.clients[platform]
        widgets = self._rows[platform]
        color = _STATE_COLORS[info.state]
        widgets.dot.style(f'color: {color}; font-size: 8px;')
        widgets.state_label.text = _STATE_LABELS[info.state]
        widgets.state_label.style(f'color: {color};')
        if widgets.btn is not None:
            widgets.btn.text = '断开' if info.state == ConnState.CONNECTED else '连接'

    def refresh_all(self):
        for p in self._rows:
            self._refresh(p)


class _RowWidgets:
    __slots__ = ('dot', 'state_label', 'host_label', 'btn')

    def __init__(self, dot, state_label, host_label, btn):
        self.dot = dot
        self.state_label = state_label
        self.host_label = host_label
        self.btn = btn
