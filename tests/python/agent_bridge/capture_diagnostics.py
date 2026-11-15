# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Actual render/pick/normal/depth checks with transformed, mirrored collection instances."""

import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from bl_agent import artifacts, capture, runtime

directory = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
directory.mkdir(parents=True, exist_ok=False)
scene = bpy.data.scenes.new('DiagnosticFixture')
source = bpy.data.collections.new('DiagnosticPrototype')
mesh = bpy.data.meshes.new('DiagnosticBox')
vertices = [(x, y, z) for x in (-.6, .6) for y in (-.4, .4) for z in (-.8, .8)]
faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
mesh.from_pydata(vertices, [], faces)
prototype = bpy.data.objects.new('DiagnosticBox', mesh)
source.objects.link(prototype)
bevel = prototype.modifiers.new('EvaluatedIteratorLifetime', 'BEVEL')
bevel.width, bevel.segments = .035, 2
instances = []
for index, x in enumerate((-1.6, 1.6)):
    obj = bpy.data.objects.new('DiagnosticInstance' + str(index), None)
    obj.instance_type = 'COLLECTION'
    obj.instance_collection = source
    obj.location.x = x
    if index:
        obj.rotation_euler.z = math.radians(30)
        obj.scale.x = -1
    scene.collection.objects.link(obj)
    instances.append(obj)
# The depsgraph uses the outer parent even for leaves two instance levels deep.
# A second nested branch shares the same prototype but is hidden for rendering.
nested = bpy.data.collections.new('NestedPrototype')
for name, hide, x in [('VisibleBranch', False, 0), ('HiddenBranch', True, 20)]:
    branch = bpy.data.objects.new(name, None)
    branch.instance_type, branch.instance_collection = 'COLLECTION', source
    branch.hide_render, branch.location.x = hide, x
    nested.objects.link(branch)
outer = bpy.data.objects.new('NestedOuter', None)
outer.instance_type, outer.instance_collection = 'COLLECTION', nested
outer.location.x = 5
scene.collection.objects.link(outer)
hidden = bpy.data.collections.new('HiddenFixture')
hidden.hide_render = True
scene.collection.children.link(hidden)
hidden_object = bpy.data.objects.new('HiddenBox', mesh)
hidden_object.location.x = 100
hidden.objects.link(hidden_object)
layer = scene.view_layers[0]
layer.update()
previous_scene = bpy.context.window.scene
before_ids = {collection: set(getattr(bpy.data, collection)) for collection in ('objects', 'meshes', 'node_groups')}
before_geometry = runtime._fingerprint(scene.objects)
status = bpy.agent_bridge.start(workspace=directory)
params = {'_job_id': 'diagnostic-fixture'}
context = runtime.JobContext(params, scene, layer)
try:
    census = bpy.agent_bridge.inspect({'context': {'scene': scene.name, 'view_layer': layer.name},
                                       'include_instances': True})
    assert census['instance_total'] == 3 and len(census['instances']) == 3, census
    from bl_agent import occurrences
    selected = occurrences.meshes(scene, layer, {bpy.data.objects['VisibleBranch']})
    assert len(selected) == 1 and selected[0]['parent'] == outer, selected
    records = capture.render(context, {'views': ['front'], 'resolution': 128,
                                       'passes': ['object_id', 'world_normal', 'depth']}, {
        'session_id': status['session_id'], 'scene_epoch': runtime._epoch,
        'revision': runtime._revision, 'frame': scene.frame_current,
    })
    table_record = next(record for record in records if record['metadata'].get('role') == 'capture_table')
    table = json.loads(artifacts.path_for(table_record['artifact_id']).read_text())
    assert len(table['objects']) == 3, table
    assert all(record['is_instance'] and record['source']['name'] == prototype.name for record in table['objects'])
    raw = {record['metadata']['role']: record for record in records if record['metadata'].get('representation') == 'raw'}
    metadata = raw['object_id']['metadata']
    camera = Matrix(metadata['camera_matrix'])
    camera_inverse = camera.inverted()
    scale, resolution = metadata['ortho_scale'], metadata['resolution']
    results = []
    for obj in instances:
        point = camera_inverse @ obj.matrix_world.translation
        x, y = int((point.x / scale + .5) * resolution), int((.5 - point.y / scale) * resolution)
        result = runtime._dispatch('capture.pick', {'artifact_id': raw['object_id']['artifact_id'], 'x': x, 'y': y})
        assert result['object']['instancer']['handle'] == runtime._handle(obj), result
        assert result['object']['source']['handle'] == runtime._handle(prototype)
        matrix = Matrix(result['object']['matrix_world'])
        assert max(abs(a - b) for ra, rb in zip(matrix, obj.matrix_world) for a, b in zip(ra, rb)) < 1e-6
        samples = {}
        for name, record in raw.items():
            image = bpy.data.images.load(str(artifacts.path_for(record['artifact_id'])), check_existing=False)
            try:
                image.colorspace_settings.name = 'Non-Color'
                offset = ((resolution - 1 - y) * resolution + x) * image.channels
                samples[name] = list(image.pixels[offset:offset + 3])
            finally:
                bpy.data.images.remove(image)
        normal = Vector(samples['world_normal'])
        expected_normal = (obj.matrix_world.to_3x3().inverted().transposed() @ Vector((0, -1, 0))).normalized()
        assert normal.dot(expected_normal) > .999, (normal, expected_normal)
        local_pixel = Vector(((x + .5) / resolution * scale - scale / 2,
                               scale / 2 - (y + .5) / resolution * scale, 0))
        origin = camera @ local_pixel
        direction = camera.to_3x3() @ Vector((0, 0, -1))
        bvh = BVHTree.FromPolygons([obj.matrix_world @ Vector(vertex) for vertex in vertices], faces)
        hit, *_ = bvh.ray_cast(origin, direction)
        expected_depth = -(camera_inverse @ hit).z
        assert abs(samples['depth'][0] - expected_depth) < .04, (samples['depth'], expected_depth)
        results.append({'instancer': obj.name, 'pixel': [x, y], 'normal': list(normal),
                        'depth': samples['depth'][0], 'expected_depth': expected_depth})
    background = runtime._dispatch('capture.pick', {'artifact_id': raw['object_id']['artifact_id'], 'x': 0, 'y': 0})
    assert background['object'] is None
    # Changing identity-dependent shaders would make a plausible but false comparison.
    material = bpy.data.materials.new('IdentityDependentFixture')
    material.use_nodes = True
    info = material.node_tree.nodes.new('ShaderNodeObjectInfo')
    material.node_tree.links.new(info.outputs['Random'],
                                 material.node_tree.nodes['Principled BSDF'].inputs['Base Color'])
    mesh.materials.append(material)
    try:
        try:
            capture.render(context, {'views': ['front'], 'resolution': 64, 'appearance': 'materials',
                                     'filename_prefix': 'rejected-'}, {})
        except ValueError as error:
            assert 'instance-dependent shader' in str(error), error
        else:
            raise AssertionError('Random material identity silently changed in the proxy capture')
    finally:
        mesh.materials.clear()
        bpy.data.materials.remove(material)
    assert before_geometry == runtime._fingerprint(scene.objects)
    assert bpy.context.window.scene == previous_scene
    assert all(set(getattr(bpy.data, name)) == values for name, values in before_ids.items())
    artifacts.seal(context.job_id)
    (directory / 'report.json').write_text(json.dumps({'samples': results, 'records': records,
                                                      'source_preserved': True, 'visible_nested_occurrences': 3,
                                                      'hidden_branch_excluded': True,
                                                      'identity_dependent_material_rejected': True}, indent=2) + '\n')
    print('AGENT_DIAGNOSTICS_OK', json.dumps(results))
finally:
    bpy.agent_bridge.stop()
