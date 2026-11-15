# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Data passes from the same frozen construction render, with capture-local correspondence."""

import json
from pathlib import Path
import uuid

import bpy

from . import artifacts


def setup(scene, passes):
    layer = scene.view_layers[0]
    layer.pass_alpha_threshold = 0.0  # Diagnostic IDs/depth refer to the first geometric surface.
    layer.use_pass_object_index = True
    layer.use_pass_normal = 'world_normal' in passes
    layer.use_pass_z = 'depth' in passes
    tree = bpy.data.node_groups.new('AgentCapture.Passes', 'CompositorNodeTree')
    scene.compositor_effects.new('AgentCapture.Passes').node_group = tree
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source = tree.nodes.new('CompositorNodeRLayers')
    source.scene = scene
    output = tree.nodes.new('NodeGroupOutput')
    tree.links.new(source.outputs['Image'], output.inputs['Image'])
    return tree, source


def outputs(tree, source, directory, passes, *, job_id, camera_distance, radius):
    """Output names are owned by one new view directory, never client filesystem paths."""
    nodes = []

    def math(operation, a, b=0):
        node = tree.nodes.new('ShaderNodeMath')
        nodes.append(node)
        node.operation = operation
        for socket, value in zip(node.inputs, (a, b)):
            if hasattr(value, 'node'):
                tree.links.new(value, socket)
            else:
                socket.default_value = value
        return node.outputs[0]

    mask = math('GREATER_THAN', source.outputs['Object Index'])
    records = []
    for name in passes:
        socket = source.outputs[{'object_id': 'Object Index', 'world_normal': 'Normal', 'depth': 'Depth'}[name]]
        if name == 'object_id':
            hue = math('FRACT', math('MULTIPLY', socket, .61803398875))
            color = tree.nodes.new('CompositorNodeCombineColor')
            nodes.append(color)
            color.mode = 'HSV'
            tree.links.new(hue, color.inputs[0])
            color.inputs[1].default_value = .7
            tree.links.new(mask, color.inputs[2])
            preview = color.outputs[0]
        elif name == 'world_normal':
            vector = tree.nodes.new('ShaderNodeVectorMath')
            nodes.append(vector)
            vector.operation = 'MULTIPLY_ADD'
            tree.links.new(socket, vector.inputs[0])
            vector.inputs[1].default_value = (.5, .5, .5)
            vector.inputs[2].default_value = (.5, .5, .5)
            multiply = tree.nodes.new('ShaderNodeVectorMath')
            nodes.append(multiply)
            multiply.operation = 'SCALE'
            tree.links.new(vector.outputs[0], multiply.inputs[0])
            tree.links.new(mask, multiply.inputs['Scale'])
            preview = multiply.outputs[0]
        else:
            preview = math('MULTIPLY', math('DIVIDE', math('SUBTRACT', camera_distance + radius, socket),
                                          max(2 * radius, .0001)), mask)
        for kind, value, extension in (('raw', socket, 'exr'), ('preview', preview, 'png')):
            node = tree.nodes.new('CompositorNodeOutputFile')
            nodes.append(node)
            node.directory = str(directory)
            node.file_name = name + '-' + kind + '-'
            node.format.media_type = 'IMAGE'
            node.format.file_format = 'OPEN_EXR' if kind == 'raw' else 'PNG'
            node.format.color_mode = 'RGB'
            node.format.color_depth = '32' if kind == 'raw' else '8'
            node.format.exr_codec = 'ZIP'
            node.save_as_render = kind != 'raw'
            if kind != 'raw':
                node.format.color_management = 'OVERRIDE'
                node.format.view_settings.view_transform = 'Raw'
            node.file_output_items.new('VECTOR' if name == 'world_normal' and kind == 'raw' else
                                       'FLOAT' if kind == 'raw' else 'RGBA', 'data')
            tree.links.new(value, node.inputs['data'])
            records.append((name, kind, extension))
            relative = (directory / (name + '-' + kind + '-data.' + extension)).relative_to(artifacts._store.root / job_id)
            artifacts.declare(job_id, [str(relative)])
    return records, nodes


def collect(context, directory, definitions, table, metadata):
    key = uuid.uuid4().hex
    common = {**metadata, 'capture_key': key, 'pixel_origin': 'top_left',
              'depth_semantics': 'Cycles camera-axis Z in scene units; 0 on miss',
              'normal_semantics': 'Cycles world shading normal; sample-averaged; edges may be non-unit',
              'id_semantics': 'Cycles unfiltered first-sample geometric hit; 0 on miss',
              'pass_alpha_threshold': 0.0}
    table_path = directory / 'objects.json'
    table_path.write_text(json.dumps({'capture_key': key, 'objects': table, 'metadata': common},
                                     separators=(',', ':'), allow_nan=False), encoding='utf8')
    result = [artifacts.register(context.job_id, table_path, 'application/json',
                                 {**common, 'role': 'capture_table'})]
    for name, kind, extension in definitions:
        paths = list(directory.glob(name + '-' + kind + '-*.' + extension))
        if len(paths) != 1:
            raise RuntimeError('Compositor did not produce exactly one owned pass: ' + name + '/' + kind)
        result.append(artifacts.register(context.job_id, paths[0], 'image/x-exr' if kind == 'raw' else 'image/png',
                                         {**common, 'role': name, 'representation': kind}))
    return result


def pick(params):
    records = artifacts.list_records()
    record = next((record for record in records if record['artifact_id'] == params.get('artifact_id')), None)
    if record is None or record['metadata'].get('role') != 'object_id' or record['metadata'].get('representation') != 'raw':
        raise ValueError('Pick requires a retained raw object-ID capture artifact')
    metadata = record['metadata']
    x, y = params.get('x'), params.get('y')
    resolution = metadata['resolution']
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < resolution for value in (x, y)):
        raise ValueError('Pick coordinates must be integer pixels inside the named capture')
    table_record = next((item for item in records if item['job_id'] == record['job_id'] and
                         item['metadata'].get('role') == 'capture_table' and
                         item['metadata'].get('capture_key') == metadata['capture_key']), None)
    if table_record is None:
        raise ValueError('Capture correspondence table is unavailable')
    table = json.loads(artifacts.path_for(table_record['artifact_id']).read_text())
    image = bpy.data.images.load(str(artifacts.path_for(record['artifact_id'])), check_existing=False)
    try:
        image.colorspace_settings.name = 'Non-Color'
        if tuple(image.size) != (resolution, resolution):
            raise ValueError('ID image dimensions differ from capture metadata')
        offset = ((resolution - 1 - y) * resolution + x) * image.channels
        index = round(image.pixels[offset])
    finally:
        bpy.data.images.remove(image)
    match = next((obj for obj in table['objects'] if obj['index'] == index), None)
    if index and match is None:
        raise ValueError('ID pixel is outside its capture table')
    # These are captured references. A caller must inspect/revalidate before editing live data.
    return {'capture_key': metadata['capture_key'], 'pixel': [x, y], 'object': match,
            'provenance': {key: metadata[key] for key in ('session_id', 'scene_epoch', 'revision', 'frame')
                           if key in metadata}, 'requires_live_revalidation': True}
