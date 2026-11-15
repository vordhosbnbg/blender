# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Built-in controls for WITH_AGENT_BRIDGE builds; no extension is involved."""

import bpy
from bpy.types import Operator, Panel
from bpy.props import StringProperty


class WM_OT_agent_bridge_start(Operator):
    bl_idname = "wm.agent_bridge_start"
    bl_label = "Start Native Agent Bridge"
    bl_description = "Allow a trusted local client to inspect Blender and execute registered recipes"

    workspace: StringProperty(name="Workspace", subtype='DIR_PATH')

    @classmethod
    def poll(cls, context):
        return not bpy.agent_bridge.status()["running"]

    def execute(self, context):
        try:
            info = bpy.agent_bridge.start(workspace=bpy.path.abspath(self.workspace) if self.workspace else None)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Agent session: " + info['descriptor'])
        return {'FINISHED'}


class WM_OT_agent_bridge_stop(Operator):
    bl_idname = "wm.agent_bridge_stop"
    bl_label = "Stop Native Agent Bridge"

    @classmethod
    def poll(cls, context):
        return bpy.agent_bridge.status()["running"]

    def execute(self, context):
        bpy.agent_bridge.stop()
        return {'FINISHED'}


class WM_OT_agent_bridge_job_control(Operator):
    bl_idname = 'wm.agent_bridge_job_control'
    bl_label = 'Control Agent Job'

    job_id: StringProperty()
    method: StringProperty()

    def execute(self, context):
        if self.method not in {'job.cancel', 'job.apply', 'job.discard'}:
            return {'CANCELLED'}
        try:
            bpy.agent_bridge.request(self.method, {'job_id': self.job_id})
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class WM_OT_agent_bridge_view_artifact(Operator):
    bl_idname = 'wm.agent_bridge_view_artifact'
    bl_label = 'Open Agent Evidence'

    artifact_id: StringProperty()

    def execute(self, context):
        from bl_agent import artifacts
        try:
            path = artifacts.path_for(self.artifact_id)
            bpy.ops.wm.path_open(filepath=str(path))
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class WM_OT_agent_bridge_restore(Operator):
    bl_idname = 'wm.agent_bridge_restore'
    bl_label = 'Restore Agent Checkpoint'

    checkpoint_id: StringProperty()

    def execute(self, context):
        import uuid
        from bl_agent import workers
        checkpoint = workers._checkpoints.get(self.checkpoint_id)
        if checkpoint is None:
            self.report({'ERROR'}, 'Checkpoint is unavailable in this session')
            return {'CANCELLED'}
        try:
            current = bpy.agent_bridge.inspect({'context': checkpoint['context'], 'fingerprint': True})
            bpy.agent_bridge.request('job.submit', {
                'operation': 'checkpoint.restore', 'operation_key': 'ui-restore-' + uuid.uuid4().hex,
                'execution_mode': 'live', 'context': checkpoint['context'], 'scope': checkpoint['scope'],
                'session_id': current['session_id'], 'scene_epoch': current['scene_epoch'],
                'expected_revision': current['revision'], 'expected_fingerprint': current['fingerprint'],
                'checkpoint_id': self.checkpoint_id,
            })
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class VIEW3D_PT_agent_bridge(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Agent"
    bl_label = "Native Agent Bridge"

    def draw(self, context):
        layout = self.layout
        status = bpy.agent_bridge.status()
        if status['running']:
            layout.operator('wm.agent_bridge_stop')
            layout.label(text=f"Clients: {status['clients']}  Jobs: {status['jobs']}")
            layout.label(text=f"Queued: {status['pending']}")
            if not status['journal_healthy']:
                layout.label(text='Journal: ' + status['journal_state'], icon='INFO')
            layout.label(text=status['descriptor'])
            from bl_agent import artifacts
            for job in bpy.agent_bridge.jobs(limit=8):
                box = layout.box()
                box.label(text=job['label'] or job['job_id'])
                percent = '' if job['progress'] is None else f" {job['progress'] * 100:.0f}%"
                box.label(text=job['state'] + ': ' + job['phase'] + percent)
                row = box.row(align=True)
                methods = [('Cancel', 'job.cancel')] if job['state'] in {'queued', 'running', 'applying'} else []
                if job['state'] == 'ready':
                    methods = [('Apply', 'job.apply'), ('Discard', 'job.discard')]
                for label, method in methods:
                    control = row.operator('wm.agent_bridge_job_control', text=label)
                    control.job_id, control.method = job['job_id'], method
                if job['state'] in {'ready', 'succeeded'}:
                    detail = bpy.agent_bridge.request('job.status', {'job_id': job['job_id']})
                    value = detail.get('outcome', {}).get('result', {}).get('value', {})
                    if isinstance(value, dict) and value.get('checkpoint_id'):
                        restore = box.operator('wm.agent_bridge_restore', text='Restore Previous Module')
                        restore.checkpoint_id = value['checkpoint_id']
                    images = [record for record in artifacts.list_records(job['job_id'])
                              if record['media_type'] == 'image/png']
                    for record in images[:3]:
                        button = box.operator('wm.agent_bridge_view_artifact',
                                              text=record['metadata'].get('view', 'Preview'))
                        button.artifact_id = record['artifact_id']
        else:
            layout.label(text="Stopped")
            layout.operator('wm.agent_bridge_start')


classes = (WM_OT_agent_bridge_start, WM_OT_agent_bridge_stop, WM_OT_agent_bridge_job_control,
           WM_OT_agent_bridge_view_artifact, WM_OT_agent_bridge_restore, VIEW3D_PT_agent_bridge)
