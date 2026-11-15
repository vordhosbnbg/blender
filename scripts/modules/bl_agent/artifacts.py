# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Owned artifact handles with persistent indices and bounded cache admission."""

import base64
import json
from pathlib import Path
import uuid

from .storage import Store, StoreError, file_hash, signature

_store = None
_archives = {}


def configure(root, limits=None, *, workspace=None):
    global _store, _archives
    _store = Store(root, limits, workspace=workspace)
    _archives = {}


def admit(job_id, budget=None):
    return _store.admit(job_id, budget)


def output_path(job_id, filename):
    return _store.allocate(job_id, filename)


def output_directory(job_id, name):
    return _store.directory(job_id, name)


def job_directory(job_id):
    """Private worker files have declared names; arbitrary added files stay unmanaged."""
    directory = admit(job_id)
    _store.declare(job_id, ['input.blend', 'current-input.blend', 'worker-input.json', 'worker.log',
                           'candidate.blend', 'worker-result.json', 'worker-result.tmp',
                           'progress.json', 'progress.tmp', 'cancel', 'input.blend@',
                           'current-input.blend@', 'candidate.blend@'])
    return directory


def check(job_id):
    return _store.check(job_id)


def declare(job_id, paths):
    _store.declare(job_id, paths)


def copy_input(job_id, source, filename, expected=None):
    source = Path(source)
    before = source.stat()
    if before.st_size > _store.policy['file_bytes'] or check(job_id) + before.st_size > _store.jobs[job_id]['budget']:
        raise StoreError('RESOURCE_LIMIT', 'Frozen input exceeds its registered storage budget')
    destination = output_path(job_id, filename)
    import hashlib
    digest = hashlib.sha256()
    try:
        with source.open('rb') as stream, destination.open('xb') as target:
            while chunk := stream.read(1024 * 1024):
                if target.tell() + len(chunk) > before.st_size:
                    raise StoreError('RECIPE_CHANGED', 'Input grew during freezing')
                digest.update(chunk)
                target.write(chunk)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise StoreError('RECIPE_CHANGED', 'Input changed while freezing')
        value = digest.hexdigest()
        if expected is not None and value != expected:
            raise StoreError('RECIPE_CHANGED', 'Input hash differs from the registered version')
        return destination, value
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def seal(job_id):
    _store.seal(job_id)


def abort(job_id):
    return _store.abort(job_id)


def close():
    if _store is not None:
        _store.close()


def stats():
    return _store.stats()


def worker_policy(job_id):
    used = _store.check(job_id)
    available = _store.jobs[job_id]['budget'] - used
    if available < 65536:
        raise StoreError('RESOURCE_LIMIT', 'Worker inputs leave less than 64 KiB for outputs')
    budget = available - max(65536, min(64 * 1024 * 1024, available // 8))
    if budget < 65536:
        raise StoreError('RESOURCE_LIMIT', 'Worker output budget leaves insufficient index overhead')
    return {'job_bytes': budget, 'file_bytes': min(budget, _store.policy['file_bytes']),
            'session_bytes': available, 'workspace_bytes': available}


def _lookup(artifact_id):
    if artifact_id in _store.records:
        return _store, _store.records[artifact_id]
    for root, records in _archives.values():
        if artifact_id in records:
            return root, records[artifact_id]
    raise StoreError('UNKNOWN_ARTIFACT', 'Artifact is unavailable; load its retained session index if needed')


def path_for(artifact_id):
    store, entry = _lookup(artifact_id)
    if isinstance(store, Store):
        path = store.path(entry['path'], exists=True)
    else:
        root = store
        path = root / entry['path']
        if any(part.is_symlink() for part in (path, *path.parents)) or not path.resolve(strict=True).is_relative_to(root):
            raise StoreError('INVALID_ARTIFACT', 'Archived artifact path escaped its root')
    if signature(path) != entry['signature']:
        raise StoreError('INVALID_ARTIFACT', 'Artifact changed after registration')
    return path


def register(job_id, path, media_type, metadata=None):
    path = Path(path).absolute()
    relative = path.relative_to(_store.root)
    if not relative.parts or relative.parts[0] != job_id:
        raise StoreError('INVALID_ARTIFACT', 'Artifact must belong to its declared job directory')
    path = _store.path(relative, exists=True)
    info = signature(path)
    _store.check(job_id)
    if len(_store.records) >= 512:
        raise StoreError('RESOURCE_LIMIT', 'Session artifact count limit reached')
    if len(json.dumps(metadata or {}, allow_nan=False)) > 16384:
        raise StoreError('RESOURCE_LIMIT', 'Artifact metadata exceeds 16 KiB; use a report artifact')
    artifact_id = uuid.uuid4().hex
    record = {'artifact_id': artifact_id, 'job_id': job_id, 'media_type': media_type,
              'bytes': info[2], 'sha256': file_hash(path), 'metadata': metadata or {}}
    _store.records[artifact_id] = {'record': record, 'path': str(relative), 'signature': info}
    try:
        _store.save()
    except Exception:
        del _store.records[artifact_id]
        raise
    return record


def archive_root(session_id):
    if not isinstance(session_id, str) or len(session_id) != 64 or any(char not in '0123456789abcdef' for char in session_id):
        raise StoreError('INVALID_ARTIFACT', 'Archive lookup needs a session ID, not a filesystem path')
    root = _store.root.parent / session_id
    if root == _store.root:
        raise StoreError('INVALID_ARTIFACT', 'Use the current store for the active session')
    if root.is_symlink() or root.resolve(strict=True).parent != _store.root.parent:
        raise StoreError('INVALID_ARTIFACT', 'Archive root is not a direct owned session directory')
    return root


def open_archive(session_id):
    root = archive_root(session_id)
    data = Store.load_index(root)
    if len(_archives) >= 8 and session_id not in _archives:
        _archives.pop(next(iter(_archives)))
    _archives[session_id] = (root, data['records'])


def list_records(job_id=None, *, session_id=None):
    if session_id and session_id != _store.root.name:
        open_archive(session_id)
        records = _archives[session_id][1]
    else:
        records = _store.records
    return [entry['record'] for entry in records.values()
            if job_id is None or entry['record']['job_id'] == job_id]


def read(artifact_id, offset=0, limit=1024 * 1024):
    _, entry = _lookup(artifact_id)
    record, path = entry['record'], path_for(artifact_id)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1024 * 1024))
    with path.open('rb') as stream:
        stream.seek(offset)
        data = stream.read(limit)
    return {'artifact_id': artifact_id, 'offset': offset, 'encoding': 'base64',
            'data': base64.b64encode(data).decode('ascii'),
            'next_offset': offset + len(data) if offset + len(data) < record['bytes'] else None}
