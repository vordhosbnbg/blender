# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""One bounded visibility census for inspection and construction capture.

Uses the selected view layer's VIEWPORT depsgraph. Render-only modifier changes are
outside this profile. Nested collection visibility is checked along persistent-ID
ancestry because the depsgraph reports the outer instancer as every occurrence's parent.
"""


def members(collection, *, instances=False, seen=None):
    seen = set() if seen is None else seen
    if collection in seen or collection.hide_render:
        return set()
    seen.add(collection)
    result = {obj for obj in collection.objects if not obj.hide_render}
    for child in collection.children:
        result.update(members(child, instances=instances, seen=seen))
    if instances:
        for obj in tuple(result):
            if obj.instance_type == 'COLLECTION' and obj.instance_collection:
                result.update(members(obj.instance_collection, instances=True, seen=seen))
    return result


def key(instance):
    original = instance.object.original
    parent = instance.parent.original if instance.is_instance and instance.parent else None
    return (original.session_uid, parent.session_uid if parent else None, bool(instance.is_instance),
            tuple(instance.persistent_id) if instance.is_instance else None)


def meshes(scene, view_layer, selected=None):
    eligible = members(scene.collection)
    collection_members, rows, blocked, chosen = {}, [], set(), set()
    for index, instance in enumerate(view_layer.depsgraph.object_instances):
        if index >= 16384:
            raise ValueError('Evaluation exceeds 16384 occurrences; select a smaller scene/view layer')
        evaluated, original = instance.object, instance.object.original
        parent = instance.parent.original if instance.is_instance and instance.parent else None
        persistent = tuple(value for value in instance.persistent_id if value != 2147483647) if parent else ()
        group = parent.as_pointer() if parent else None
        hidden = original.hide_render or not original.visible_camera
        if parent:
            hidden |= parent not in eligible or parent.hide_render or not parent.visible_camera
            if parent.instance_type == 'COLLECTION' and parent.instance_collection:
                collection = parent.instance_collection
                if collection not in collection_members:
                    collection_members[collection] = members(collection, instances=True)
                hidden |= original not in collection_members[collection]
            if original.instance_type == 'COLLECTION' and original.instance_collection:
                hidden |= original.instance_collection.hide_render
            if hidden:
                blocked.add((group, persistent))
            if selected is not None and original in selected:
                chosen.add((group, persistent))
        else:
            hidden |= original not in eligible
        if original.type != 'MESH' or not instance.show_self or hidden:
            continue
        # Evaluated dupli objects belong to the iterator and can be reused/freed
        # on its next step. Retain original IDs and copied values only.
        rows.append({'key': key(instance), 'original': original, 'parent': parent,
                     'vertices': len(evaluated.data.vertices), 'faces': len(evaluated.data.polygons),
                     'persistent': persistent, 'group': group,
                     'persistent_id': list(instance.persistent_id) if parent else None,
                     'matrix': instance.matrix_world.copy(), 'is_instance': bool(instance.is_instance)})
    result = []
    for row in rows:
        ancestry = [(row['group'], row['persistent'][index:]) for index in range(len(row['persistent']))]
        if any(key in blocked for key in ancestry):
            continue
        if selected is not None and row['original'] not in selected and row['parent'] not in selected:
            if not any(key in chosen for key in ancestry):
                continue
        if len(result) >= 4096:
            raise ValueError('Capture exceeds 4096 evaluated mesh occurrences; select a smaller scope')
        result.append(row)
    return result


def evaluated_meshes(scene, view_layer, selected=None):
    """Yield an evaluated object only while its current iterator entry remains alive."""
    rows = meshes(scene, view_layer, selected)
    pending = {row['key']: row for row in rows}
    if len(pending) != len(rows):
        raise ValueError('Evaluated occurrences have ambiguous identity in this capture profile')
    for instance in view_layer.depsgraph.object_instances:
        row = pending.pop(key(instance), None)
        if row is not None:
            yield instance.object, row
    if pending:
        raise ValueError('Evaluation changed between visibility census and mesh capture')
