from nicegui import ui

from ui.services.script_service import ScriptService, RunState

_HEADER_SLOT = '''
<div class="row items-center no-wrap" style="width:100%">
    <q-icon v-if="props.node.children"
            name="folder_open" size="xs" class="q-mr-xs"
            style="color:#4a90d9" />
    <q-icon v-else
            name="description" size="xs" class="q-mr-xs"
            style="color:#7f8c9b" />
    <span style="font-size:13px">{{ props.node.label }}</span>
    <q-space />
    <q-btn v-if="props.node.id.endsWith('.py')"
           flat dense round size="xs"
           icon="play_arrow" color="green"
           class="q-ml-xs"
           style="opacity:0.6"
           @mouseenter="$event.target.style.opacity=1"
           @mouseleave="$event.target.style.opacity=0.6"
           @click.stop="() => $parent.$emit('run_script', props.node.id)" />
</div>
'''


class ScriptPanel:
    def __init__(self, script_service: ScriptService):
        self._svc = script_service
        self._build()

    def _build(self):
        with ui.column().classes('w-full h-full gap-0'):
            ui.label('脚本列表').classes('text-subtitle1 font-medium px-4 py-2')

            tree_data = self._svc.scan()
            tree = ui.tree(
                tree_data, label_key='label', node_key='id',
            ).classes('flex-grow px-2').style('color: #2c3e50;')
            tree.props('dense')
            tree.add_slot('default-header', _HEADER_SLOT)
            tree.on('run_script', self._on_run_click)

    def _on_run_click(self, e):
        script_id = e.args
        if self._svc.state == RunState.RUNNING:
            if self._svc.running_id == script_id:
                self._svc.stop_script()
            else:
                ui.notify('已有脚本在运行中', type='warning')
        else:
            self._svc.run_script(script_id)
