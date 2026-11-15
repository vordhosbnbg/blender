# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Small deterministic fixtures; real town adapters stay in the asset repository."""

import time
import bpy


def create(context):
    collection = bpy.data.collections.get("BridgeFixture")
    if collection is None:
        collection = bpy.data.collections.new("BridgeFixture")
        context.scene.collection.children.link(collection)
    mesh = bpy.data.meshes.new("FixtureMesh")
    mesh.from_pydata(
        [(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)], [],
        [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)])
    mesh.update()
    obj = bpy.data.objects.new("Fixture", mesh)
    collection.objects.link(obj)
    obj.location.x = context.parameters.get("offset", 0)
    return {"name": obj.name, "faces": len(mesh.polygons), "dirty": bpy.data.is_dirty}


def cooperative(context):
    for index in range(100):
        context.progress("cooperative", index / 100)
        time.sleep(0.01)
    return {"finished": True}


def context_probe(context):
    assert bpy.context.scene == context.scene
    assert bpy.context.view_layer == context.view_layer
    assert not bpy.context.window or bpy.context.window.scene == context.scene
    return {"scene": context.scene.name, "evaluated_scene": bpy.context.evaluated_depsgraph_get().scene.name}


def large_result(context):
    return {"text": 'x' * 100000}


def partial_failure(context):
    create(context)
    raise RuntimeError('Deliberate failure after a live edit')


def instances(context):
    source = bpy.data.collections.new('Fixture.Instance.Source')
    mesh = bpy.data.meshes.new('Fixture.Instance.Box')
    mesh.from_pydata([(x, y, z) for x in (-.6, .6) for y in (-.4, .4) for z in (-.8, .8)], [],
                     [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
    prototype = bpy.data.objects.new('Fixture.Instance.Prototype', mesh)
    source.objects.link(prototype)
    values = []
    for x in (-3, 3):
        obj = bpy.data.objects.new('Fixture.Instance', None)
        obj.instance_type = 'COLLECTION'
        obj.instance_collection = source
        obj.location.x = x
        context.scene.collection.objects.link(obj)
        values.append(context.object_reference(obj))
    return {'instances': values, 'prototype': context.object_reference(prototype)}


def move_instance(context):
    obj = context.resolve_object(context.parameters['handle'])
    obj.location.x += context.parameters['offset']
    return context.object_reference(obj)


def unicode_progress(context):
    context.progress('繰' * 80, .5)
    return True


def register():
    for name, function in (("fixture.create", create), ("fixture.cooperative", cooperative),
                           ("fixture.context_probe", context_probe), ("fixture.large_result", large_result),
                           ("fixture.partial_failure", partial_failure)):
        bpy.agent_bridge.register_recipe({
            "id": name, "version": "1", "scope": {"collection": "BridgeFixture"},
            "parameters": {"type": "object", "properties": {"offset": {"type": "number"}}},
        }, function)
    for name, function in (('fixture.instances', instances), ('fixture.move_instance', move_instance),
                           ('fixture.unicode_progress', unicode_progress)):
        bpy.agent_bridge.register_recipe({'id': name, 'version': '1', 'scope': {'collection': 'Fixture.Instance'},
                                          'parameters': {'type': 'object'}}, function)
