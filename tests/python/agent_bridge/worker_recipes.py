# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import time
import bpy

SCOPE = {'collection_name': 'WorkerFixture'}


def build(context):
    if context.parameters.get('write_bytes', 0):
        from bl_agent import artifacts
        path = artifacts.output_path(context.job_id, 'oversized.bin')
        with path.open('wb') as stream:
            stream.write(b'x' * context.parameters['write_bytes'])
    delay = context.parameters.get('delay_s', 0)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        context.progress('deliberate_test_delay', 0.1)
        time.sleep(0.02)
    collection = bpy.data.collections.get(SCOPE['collection_name'])
    if collection is None:
        collection = bpy.data.collections.new(SCOPE['collection_name'])
        context.scene.collection.children.link(collection)
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    if context.parameters.get('remove', False):
        return {'removed': True, 'listener_started': bpy.agent_bridge.status()['running']}
    width = context.parameters.get('width', 1)
    mesh = bpy.data.meshes.new('WorkerFixture.Mesh')
    mesh.from_pydata([(x * width, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)], [],
                     [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)])
    mesh.update()
    if context.parameters.get('invalid_mesh', False):
        mesh.vertices[0].co.x = float('nan')
    obj = bpy.data.objects.new('WorkerBlock', mesh)
    obj['part_key'] = 'block'
    collection.objects.link(obj)
    if context.parameters.get('modifier', False):
        obj.modifiers.new('Unsupported added stack', 'BEVEL')
    if context.parameters.get('bevel', False):
        modifier = obj.modifiers.new('Supported bevel', 'BEVEL')
        modifier.width = 0.04
        modifier.segments = 2
    result = {'name': obj.name, 'width': width, 'listener_started': bpy.agent_bridge.status()['running']}
    if context.parameters.get('nested_result', False):
        for _ in range(80):
            result = {'nested': result}
    return result


def many_artifacts(context):
    result = build(context)
    from bl_agent import artifacts
    for index in range(20):
        path = artifacts.output_path(context.job_id, f'data-{index}.bin')
        path.write_bytes(bytes([index]))
        artifacts.register(context.job_id, path, 'application/octet-stream', {'text': 'x' * 8000})
    return {**result, 'files': 20}


def register():
    bpy.agent_bridge.register_recipe({
        'id': 'fixture.worker', 'version': '1', 'scope': SCOPE, 'object_key': 'part_key',
        'execution_modes': ['worker_candidate'],
        'worker_timeout_s': 2,
        'storage_budget_bytes': 524288,
        'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
            'width': {'type': 'number', 'minimum': 0.1, 'maximum': 5},
            'delay_s': {'type': 'number', 'minimum': 0, 'maximum': 30},
            'remove': {'type': 'boolean'}, 'modifier': {'type': 'boolean'},
            'bevel': {'type': 'boolean'},
            'nested_result': {'type': 'boolean'},
            'write_bytes': {'type': 'integer', 'minimum': 0, 'maximum': 2097152},
            'invalid_mesh': {'type': 'boolean'},
        }},
    }, build)
    bpy.agent_bridge.register_recipe({
        'id': 'fixture.worker_artifacts', 'version': '1', 'scope': SCOPE, 'object_key': 'part_key',
        'execution_modes': ['worker_candidate'], 'worker_timeout_s': 10,
        'storage_budget_bytes': 8 * 1024 * 1024,
        'parameters': {'type': 'object'},
    }, many_artifacts)
