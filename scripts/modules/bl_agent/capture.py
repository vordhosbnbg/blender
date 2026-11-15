# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Construction captures from evaluated geometry in a temporary render scene."""

import hashlib
import json

import bpy
from mathutils import Vector
from . import artifacts
from . import occurrences


def _reference(context, obj):
    function = getattr(context, 'object_reference', None)
    return function(obj) if function else {'domain': 'isolated_process', 'name': obj.name}


def _copy_geometry(context, scene, handles, owned_objects, meshes, indexed):
    graph = context.view_layer.depsgraph
    selected = {context.resolve_object(handle) for handle in handles} if handles else None
    cache, table = {}, []
    for evaluated, occurrence in occurrences.evaluated_meshes(context.scene, context.view_layer, selected):
        original, parent = occurrence['original'], occurrence['parent']
        cache_key = (original.as_pointer(), evaluated.data.as_pointer())
        mesh = cache.get(cache_key)
        if mesh is None:
            mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=graph)
            meshes.append(mesh)
            cache[cache_key] = mesh
        copy = bpy.data.objects.new('Capture.' + original.name, mesh)
        owned_objects.append(copy)
        matrix = occurrence['matrix']
        copy.matrix_world = matrix
        copy.color = original.color
        copy.pass_index = len(table) + 1 if indexed else original.pass_index
        for target, source in zip(copy.material_slots, original.material_slots):
            target.link = source.link
            target.material = source.material
        for key in original.keys():
            value = original[key]
            copy[key] = value.to_dict() if hasattr(value, 'to_dict') else value.to_list() if hasattr(value, 'to_list') else value
        scene.collection.objects.link(copy)
        table.append({'index': len(table) + 1, 'source': _reference(context, original),
                      'instancer': _reference(context, parent) if parent else None,
                      'persistent_id': occurrence['persistent_id'],
                      'matrix_world': [list(row) for row in matrix],
                      'is_instance': occurrence['is_instance']})
    return table, graph.mode


def _check_material_profile(objects, indexed):
    """Proxy meshes cannot preserve instance identity-dependent shader inputs."""
    visited = set()

    def check(tree):
        if tree is None or tree in visited:
            return
        visited.add(tree)
        for node in tree.nodes:
            unsupported = False
            if node.bl_idname == 'ShaderNodeObjectInfo':
                names = {'Random'} | ({'Object Index'} if indexed else set())
                unsupported = any(output.name in names and output.is_linked for output in node.outputs)
            elif node.bl_idname == 'ShaderNodeParticleInfo':
                unsupported = any(output.is_linked for output in node.outputs)
            elif node.bl_idname == 'ShaderNodeAttribute' and node.attribute_type == 'INSTANCER':
                unsupported = any(output.is_linked for output in node.outputs)
            elif node.bl_idname == 'ShaderNodeUVMap' and node.from_instancer:
                unsupported = any(output.is_linked for output in node.outputs)
            if unsupported:
                raise ValueError('Material capture cannot preserve instance-dependent shader node: ' + node.name)
            if node.type == 'GROUP':
                check(node.node_tree)

    for obj in objects:
        for slot in obj.material_slots:
            if slot.material and slot.material.use_nodes:
                check(slot.material.node_tree)


def render(context, params, provenance):
    views = params.get("views", ["front", "oblique", "underside"])
    directions = {"front": (0, -1, 0), "rear": (0, 1, 0), "left": (-1, 0, 0),
                  "right": (1, 0, 0), "oblique": (1.3, -1.8, 1.15), "underside": (1, -1, -1)}
    if not views or len(views) > 8 or any(view not in directions for view in views) or len(set(views)) != len(views):
        raise ValueError("Choose up to eight named construction views")
    resolution = int(params.get("resolution", 512))
    if not 64 <= resolution <= 2048:
        raise ValueError("Capture resolution must be between 64 and 2048")
    engine = params.get("engine", "CYCLES")
    if engine not in {"CYCLES", "BLENDER_WORKBENCH"}:
        raise ValueError("Construction capture supports CYCLES or BLENDER_WORKBENCH")
    appearance = params.get('appearance', 'clay')
    if appearance not in {'clay', 'materials'} or (appearance == 'materials' and engine != 'CYCLES'):
        raise ValueError('Material captures require Cycles; Workbench is a clay diagnostic')
    prefix = params.get('filename_prefix', '')
    if not isinstance(prefix, str) or len(prefix) > 40 or any(
            not (char.isascii() and (char.isalnum() or char in '_-')) for char in prefix):
        raise ValueError('Capture prefix must contain up to 40 ASCII letters, digits, underscores or hyphens')
    passes = params.get('passes', [])
    if not isinstance(passes, list) or any(value not in {'object_id', 'world_normal', 'depth'} for value in passes) or len(set(passes)) != len(passes):
        raise ValueError('Passes must be unique object_id, world_normal or depth names')
    if passes and engine != 'CYCLES':
        raise ValueError('Data passes require Cycles')
    context.view_layer.update()
    artifacts.admit(context.job_id)
    # Conservative raw-buffer bound for the controlled native render writes. This is admission
    # accounting, not an OS/filesystem quota for arbitrary trusted Python file I/O.
    estimate = len(views) * (resolution * resolution * (4 + len(passes) * 16) + 65536)
    if artifacts.check(context.job_id) + estimate > artifacts._store.jobs[context.job_id]['budget']:
        from .storage import StoreError
        raise StoreError('RESOURCE_LIMIT', 'Capture buffers exceed the owned job budget; reduce scope/resolution')
    scene = bpy.data.scenes.new("AgentCapture." + context.job_id)
    scene["_agent_capture"] = True
    owned_objects, meshes, light_data, cameras = [], [], [], []
    world = None
    material = None
    tree = None
    try:
        scene.unit_settings.system = context.scene.unit_settings.system
        scene.unit_settings.scale_length = context.scene.unit_settings.scale_length
        scene.render.fps, scene.render.fps_base = context.scene.render.fps, context.scene.render.fps_base
        scene.frame_set(context.scene.frame_current, subframe=context.scene.frame_subframe)
        table, evaluation_mode = _copy_geometry(context, scene, params.get('handles'), owned_objects, meshes, bool(passes))
        if not table:
            raise ValueError('Capture contains no visible evaluated meshes')
        if appearance == 'materials':
            _check_material_profile(owned_objects, bool(passes))
        geometry_count = len(owned_objects)
        scene.view_layers[0].update()
        corners = [obj.matrix_world @ Vector(corner) for obj in owned_objects for corner in obj.bound_box]
        lower = Vector(tuple(min(point[axis] for point in corners) for axis in range(3)))
        upper = Vector(tuple(max(point[axis] for point in corners) for axis in range(3)))
        center = (lower + upper) / 2
        radius = max((point - center).length for point in corners)
        scene.render.engine = engine
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 16
        scene.cycles.seed = 0
        scene.render.resolution_x = scene.render.resolution_y = resolution
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        scene.render.film_transparent = bool(params.get("transparent", False))
        scene.view_settings.exposure = 0.0
        world = bpy.data.worlds.new("AgentCapture.World")
        world.use_nodes = True
        world.color = (0.12, 0.12, 0.12)
        world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.15, 0.15, 0.15, 1)
        world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.35
        scene.world = world
        material = bpy.data.materials.new("AgentCapture.Clay")
        material.use_nodes = True
        surface = material.node_tree.nodes.get('Principled BSDF')
        surface.inputs['Base Color'].default_value = (0.38, 0.42, 0.48, 1)
        surface.inputs['Roughness'].default_value = 0.7
        if appearance == 'clay':
            scene.view_layers[0].material_override = material
        if engine == 'BLENDER_WORKBENCH':
            scene.display.shading.light = 'STUDIO'
            scene.display.shading.color_type = 'SINGLE'
            scene.display.shading.single_color = (0.38, 0.42, 0.48)
            scene.display.shading.show_shadows = True
            scene.display.shading.show_cavity = True
            scene.display.shading.cavity_type = 'BOTH'
            scene.display.render_aa = '16'
        for index, direction in enumerate(((1, -2, 3), (-2, -1, 1), (0, 2, 2))):
            light = bpy.data.lights.new(f"AgentCapture.Light{index}", 'AREA')
            light.energy = radius * radius * (100 if index == 0 else 45)
            light.shape = 'DISK'
            light.size = max(radius, 0.5)
            lamp = bpy.data.objects.new(light.name, light)
            scene.collection.objects.link(lamp)
            lamp.location = center + Vector(direction).normalized() * radius * 4
            lamp.rotation_euler = (center - lamp.location).to_track_quat('-Z', 'Y').to_euler()
            light_data.append(light)
            owned_objects.append(lamp)
        camera_data = bpy.data.cameras.new("AgentCapture.Camera")
        cameras.append(camera_data)
        camera = bpy.data.objects.new(camera_data.name, camera_data)
        owned_objects.append(camera)
        scene.collection.objects.link(camera)
        camera_data.type = 'ORTHO'
        camera_data.ortho_scale = max(radius * 2.2, 0.1)
        camera_data.clip_end = max(radius * 20, 100)
        scene.camera = camera
        if passes:
            from . import capture_data
            tree, source = capture_data.setup(scene, passes)
        results = []
        for index, view in enumerate(views):
            context.progress("capture:" + view, index / len(views))
            camera.location = center + Vector(directions[view]).normalized() * max(radius * 4, 1)
            camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
            # Diagnostic illumination follows the camera so an underside is actually inspectable.
            # Beauty/material comparison requires its own explicitly fixed lighting contract.
            for lamp, direction in zip(owned_objects[geometry_count:geometry_count + 3],
                                       ((-1, 2, 3), (2, -1, 2), (-1, -2, 1))):
                lamp.location = center + camera.rotation_euler.to_quaternion() @ (
                    Vector(direction).normalized() * radius * 4)
                lamp.rotation_euler = (center - lamp.location).to_track_quat('-Z', 'Y').to_euler()
            path = artifacts.output_path(context.job_id, prefix + view + '.png')
            scene.render.filepath = str(path)
            # Matrix evaluation is lazy outside the active window scene. Record the evaluated
            # camera, rather than the previous view (or its initial identity matrix).
            scene.view_layers[0].update()
            if passes:
                directory = artifacts.output_directory(context.job_id, prefix + view + '-passes')
                definitions, pass_nodes = capture_data.outputs(tree, source, directory, passes,
                                                                job_id=context.job_id,
                                                                camera_distance=(camera.location - center).length,
                                                                radius=radius)
            bpy.ops.render.render(scene=scene.name, write_still=True)
            metadata = {
                **provenance, "view": view, "projection": "ORTHO", "ortho_scale": camera_data.ortho_scale,
                "camera_matrix": [list(row) for row in camera.matrix_world], "resolution": resolution,
                "engine": engine, "device": "CPU" if engine == 'CYCLES' else "GPU",
                "samples": 16, "seed": 0,
                "pass": appearance, "instance_coverage": "evaluated mesh occurrences",
                "occurrences": len(table), 'evaluation_mode': evaluation_mode,
                'metres_per_unit': context.scene.unit_settings.scale_length,
                'frame': scene.frame_current, 'subframe': scene.frame_subframe,
                'view_transform': scene.view_settings.view_transform, 'look': scene.view_settings.look,
                'exposure': scene.view_settings.exposure, 'gamma': scene.view_settings.gamma,
                "lighting": "camera_relative_construction" if engine == 'CYCLES' else "workbench_studio",
                'material_profile': 'evaluated_mesh_proxy' if appearance == 'materials' else 'clay_override',
                "transparent": scene.render.film_transparent,
            }
            metadata['settings_sha256'] = hashlib.sha256(json.dumps(metadata, sort_keys=True, allow_nan=False).encode()).hexdigest()
            results.append(artifacts.register(context.job_id, path, 'image/png', metadata))
            if passes:
                results.extend(capture_data.collect(context, directory, definitions, table, metadata))
                for node in reversed(pass_nodes):
                    tree.nodes.remove(node)
        return results
    finally:
        bpy.data.scenes.remove(scene)
        for obj in owned_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in meshes:
            bpy.data.meshes.remove(mesh)
        for light in light_data:
            bpy.data.lights.remove(light)
        for camera_data in cameras:
            bpy.data.cameras.remove(camera_data)
        if world:
            bpy.data.worlds.remove(world)
        if material:
            bpy.data.materials.remove(material)
        if tree:
            bpy.data.node_groups.remove(tree)
