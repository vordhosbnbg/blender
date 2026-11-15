# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded private cache ownership, reservations and explicit release. No Blender imports."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

MIB = 1024 * 1024
DEFAULT_LIMITS = {'job_bytes': 512 * MIB, 'session_bytes': 2048 * MIB,
                  'workspace_bytes': 8192 * MIB, 'file_bytes': 256 * MIB}
OWNER = 'blender-agent-cache-v1'
INDEX = 'store.json'
LEASE = 'lease.json'


class StoreError(RuntimeError):
    def __init__(self, kind, message):
        self.kind = kind
        self.details = {}
        super().__init__(message)


def limits(values=None):
    result = dict(DEFAULT_LIMITS)
    for key, value in (values or {}).items():
        if key not in result or isinstance(value, bool) or not isinstance(value, int) or not 65536 <= value <= 1024 ** 4:
            raise ValueError('Storage limits require known byte limits from 64 KiB to 1 TiB')
        result[key] = value
    if result['file_bytes'] > result['job_bytes'] or result['job_bytes'] > result['session_bytes'] or result['session_bytes'] > result['workspace_bytes']:
        raise ValueError('Storage limits must satisfy file <= job <= session <= workspace')
    return result


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def signature(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StoreError('INVALID_ARTIFACT', 'Owned output must be a regular unshared file')
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.index-' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def scan(directory):
    """Logical bytes; do not traverse links or an unbounded cache directory."""
    result = {}
    entries = 0
    for parent, directories, files in os.walk(directory, followlinks=False):
        entries += 1 + len(files)
        if entries > 100000:
            raise StoreError('RESOURCE_LIMIT', 'Cache census exceeds 100000 filesystem entries')
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in files:
            path = Path(parent) / name
            if path.is_symlink():
                continue
            try:
                info = path.stat()
            except FileNotFoundError:
                continue  # A worker may atomically replace a progress file.
            if stat.S_ISREG(info.st_mode):
                result[path] = info.st_size
                if len(result) > 100000:
                    raise StoreError('RESOURCE_LIMIT', 'Cache census exceeds 100000 files')
    return result


def _alive(pid, start):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19] == start
    except (OSError, IndexError):
        return False


class Store:
    def __init__(self, root, values=None, *, workspace=None):
        original = Path(root).absolute()
        if original.is_symlink():
            raise StoreError('INVALID_ARTIFACT', 'Cache root cannot be a symbolic link')
        self.root = original.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace = Path(workspace).resolve() if workspace else self.root.parent
        if not self.root.is_relative_to(self.workspace):
            raise StoreError('INVALID_ARTIFACT', 'Cache root must be inside its accounting workspace')
        self.policy = limits(values)
        self.pid = os.getpid()
        self.start = Path(f'/proc/{self.pid}/stat').read_text().split(') ', 1)[1].split()[19]
        self.identity = uuid.uuid4().hex
        self.closed = False
        self.archived = False
        self.records, self.jobs = {}, {}
        with self.lock():
            index = self.root / INDEX
            if not index.exists() and any(path.name != '.storage.lock' for path in self.root.iterdir()):
                raise StoreError('UNMANAGED_FILES', 'Refusing to claim a nonempty unmanaged cache root')
            if index.exists() or index.is_symlink():
                data = self.load_index(self.root)
                self.require_inactive()
                self.identity = data['identity']
                self.policy = limits(values if values is not None else data.get('limits'))
                self.records, self.jobs = data['records'], data['jobs']
                # Reopening an inactive store does not restart old reservations or work.
                for job in self.jobs.values():
                    job['reserved'] = 0
            self.save()

    def require_inactive(self):
        lease = self.root / LEASE
        if lease.exists() or lease.is_symlink():
            signature(lease)
            if lease.stat().st_size > MIB:
                raise StoreError('INVALID_ARTIFACT', 'Cache lease exceeds 1 MiB')
            active = json.loads(lease.read_text())
            if active.get('owner') != OWNER:
                raise StoreError('INVALID_ARTIFACT', 'Unrecognized cache lease')
            if _alive(active.get('pid'), active.get('start')):
                raise StoreError('BUSY', 'Cache store is owned by a running process')

    @classmethod
    def open_inactive(cls, root, *, workspace):
        """Read a stopped session without claiming its lease or resuming any work."""
        store = cls.__new__(cls)
        store.root, store.workspace = Path(root).resolve(strict=True), Path(workspace).resolve(strict=True)
        data = cls.load_index(store.root)
        store.require_inactive()
        store.policy, store.identity = limits(data['limits']), data['identity']
        store.records, store.jobs = data['records'], data['jobs']
        for job in store.jobs.values():
            job['reserved'] = 0  # A dead writer's reservation is not a live dependency.
        store.archived, store.closed = True, True
        store.pid, store.start = 0, ''
        return store

    @staticmethod
    def load_index(root):
        path = Path(root) / INDEX
        signature(path)
        if path.stat().st_size > 16 * MIB:
            raise StoreError('INVALID_ARTIFACT', 'Cache index exceeds 16 MiB')
        data = json.loads(path.read_text())
        if data.get('owner') != OWNER or not isinstance(data.get('records'), dict) or not isinstance(data.get('jobs'), dict):
            raise StoreError('INVALID_ARTIFACT', 'Unrecognized cache ownership index')
        return data

    @contextmanager
    def lock(self):
        path = self.workspace / '.storage.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise StoreError('INVALID_ARTIFACT', 'Cache lock ownership is invalid')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save(self):
        index = {'owner': OWNER, 'identity': self.identity,
                 'records': self.records, 'jobs': self.jobs, 'limits': self.policy}
        lease = {'owner': OWNER, 'pid': self.pid, 'start': self.start,
                 'metadata_bytes': self.metadata_reserve(), 'jobs': list(self.jobs),
                 'workspace_limit': self.policy['workspace_bytes'],
                 'reserved': {name: job['reserved'] for name, job in self.jobs.items() if job['reserved']}}
        bound = self.metadata_reserve() // 4
        if len(json.dumps(index, separators=(',', ':')).encode()) > bound or len(json.dumps(lease).encode()) > bound:
            raise StoreError('RESOURCE_LIMIT', 'Persistent cache index exceeds its reserved metadata budget')
        atomic_json(self.root / INDEX, index)
        lease_key = json.dumps(lease, separators=(',', ':'))
        if not self.archived and lease_key != getattr(self, '_lease_key', None):
            atomic_json(self.root / LEASE, lease)
            self._lease_key = lease_key

    def metadata_reserve(self):
        # Covers a bounded index, its atomic-write temporary, lease and native journal.
        return max(32768, min(64 * MIB, self.policy['session_bytes'] // 8))

    def path(self, relative, *, exists=False):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise StoreError('INVALID_ARTIFACT', 'Invalid cache-relative path')
        path = self.root / relative
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != self.root.parent):
            raise StoreError('INVALID_ARTIFACT', 'Cache path contains a symbolic link')
        if not path.resolve(strict=exists).is_relative_to(self.root):
            raise StoreError('INVALID_ARTIFACT', 'Cache path escapes its root')
        return path

    def stats(self):
        files = scan(self.workspace)
        workspace_used = sum(files.values())
        # Aggregate each directory once. Repeated Path.is_relative_to() scans for
        # every session/job made census quadratic in retained cache contents.
        prefixes = {(): workspace_used}
        depth = len(self.workspace.parts)
        for path, size in files.items():
            parts = path.parts[depth:]
            for end in range(1, len(parts)):
                key = parts[:end]
                prefixes[key] = prefixes.get(key, 0) + size
        own_prefix = self.root.parts[depth:]
        used = prefixes.get(own_prefix, 0)
        remaining = 0
        effective_limit = self.policy['workspace_bytes']
        for root in [self.workspace, *[p for p in self.workspace.iterdir() if p.is_dir() and not p.is_symlink()]]:
            lease = root / LEASE
            if not lease.is_file() or lease.is_symlink() or lease.stat().st_size > MIB:
                continue
            try:
                data = json.loads(lease.read_text())
                if data.get('owner') != OWNER or not _alive(data['pid'], data['start']):
                    continue
                effective_limit = min(effective_limit, data.get('workspace_limit', effective_limit))
                prefix = root.parts[depth:]
                for job, amount in data['reserved'].items():
                    actual = prefixes.get((*prefix, job), 0)
                    remaining += max(0, amount - actual)
                metadata = prefixes.get(prefix, 0) - sum(prefixes.get((*prefix, job), 0)
                                                        for job in data.get('jobs', data['reserved']))
                remaining += max(0, data.get('metadata_bytes', 0) - metadata)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        session_remaining = sum(max(0, job['reserved'] - prefixes.get((*own_prefix, name), 0))
                                for name, job in self.jobs.items())
        metadata = used - sum(prefixes.get((*own_prefix, name), 0) for name in self.jobs)
        if not self.closed:
            session_remaining += max(0, self.metadata_reserve() - metadata)
        return {'used_bytes': used, 'workspace_used_bytes': workspace_used,
                'reserved_remaining_bytes': session_remaining, 'workspace_reserved_remaining_bytes': remaining,
                'workspace_effective_limit': effective_limit,
                'limits': self.policy, 'records': len(self.records), 'jobs': len(self.jobs)}

    def admit(self, name, budget=None):
        if self.archived:
            raise StoreError('INVALID_ARTIFACT', 'Archived stores cannot admit new jobs')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', name):
            raise StoreError('INVALID_ARTIFACT', 'Invalid owned job directory name')
        budget = self.jobs.get(name, {}).get('budget', self.policy['job_bytes']) if budget is None else budget
        if isinstance(budget, bool) or not isinstance(budget, int) or not 65536 <= budget <= self.policy['job_bytes']:
            raise StoreError('RESOURCE_LIMIT', 'Job budget is outside the configured cache limits')
        with self.lock():
            job = self.jobs.get(name)
            if not job and len(self.jobs) >= 128:
                raise StoreError('RESOURCE_LIMIT', 'Cache job-count limit reached')
            if job and job['reserved']:
                return self.path(name, exists=True)
            stats = self.stats()
            actual = sum(scan(self.root / name).values()) if job else 0
            reserve = max(0, budget - actual)
            if stats['used_bytes'] + stats['reserved_remaining_bytes'] + reserve > self.policy['session_bytes']:
                raise StoreError('RESOURCE_LIMIT', 'Session cache admission limit reached; release unused jobs')
            if stats['workspace_used_bytes'] + stats['workspace_reserved_remaining_bytes'] + reserve > stats['workspace_effective_limit']:
                raise StoreError('RESOURCE_LIMIT', 'Workspace cache admission limit reached; release unused jobs')
            directory = self.path(name)
            if directory.exists() and not job:
                raise StoreError('INVALID_ARTIFACT', 'Refusing to claim an existing unmanaged directory')
            directory.mkdir(exist_ok=True, mode=0o700)
            if job is None:
                job = self.jobs[name] = {'budget': budget, 'reserved': budget, 'allocated': [],
                                       'directories': [], 'inventory': {}}
            job['reserved'] = budget
            self.save()
        return directory

    def allocate(self, job, filename):
        if not isinstance(filename, str) or Path(filename).name != filename or filename in {'.', '..', ''}:
            raise StoreError('INVALID_ARTIFACT', 'Invalid artifact filename')
        directory = self.admit(job)
        path = self.path(Path(job) / filename)
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
        relative = str(path.relative_to(self.root))
        if relative not in self.jobs[job]['allocated']:
            self.jobs[job]['allocated'].append(relative)
            self.save()
        return path

    def declare(self, job, names):
        self.admit(job)
        for name in names:
            path = self.path(Path(job) / name)
            relative = str(path.relative_to(self.root))
            if relative not in self.jobs[job]['allocated']:
                self.jobs[job]['allocated'].append(relative)
        self.save()

    def directory(self, job, name):
        path = self.allocate(job, name)
        path.mkdir(mode=0o700)
        self.jobs[job].setdefault('directories', []).append(str(path.relative_to(self.root)))
        self.save()
        return path

    def check(self, job):
        files = scan(self.path(job, exists=True))
        budget = self.jobs[job]['budget']
        if sum(files.values()) > budget or any(size > self.policy['file_bytes'] for size in files.values()):
            raise StoreError('RESOURCE_LIMIT', 'Owned job exceeded its byte budget or per-file limit')
        return sum(files.values())

    def seal(self, job, *, check_limits=True, persist=True):
        if job not in self.jobs:
            return
        if check_limits:
            self.check(job)
        record = self.jobs[job]
        allocated = set(record['allocated'])
        registered = {}
        for item in self.records.values():
            registered.setdefault(item['path'], set()).add(tuple(item['signature']))
        allocated.update(item['path'] for item in self.records.values() if item['record']['job_id'] == job)
        child_root = self.root / job / 'worker-artifacts'
        if (child_root / INDEX).is_file():
            child = self.load_index(child_root)
            child_owned = [INDEX, LEASE, '.storage.lock']
            child_owned.extend(item['path'] for item in child['records'].values())
            child_owned.extend(path for entry in child['jobs'].values() for path in entry['inventory'])
            child_owned.extend(path for entry in child['jobs'].values() for path in entry['allocated'])
            allocated.update(str((child_root / path).relative_to(self.root)) for path in child_owned)
        if child_root.exists():
            for path in child_root.glob('.index-*.tmp'):
                if re.fullmatch(r'\.index-[0-9a-f]{32}\.tmp', path.name):
                    allocated.add(str(path.relative_to(self.root)))
        for relative in allocated:
            path = self.path(relative)
            if path.is_file():
                current = signature(path)
                if relative in registered and registered[relative] != {tuple(current)}:
                    raise StoreError('INVALID_ARTIFACT', 'Artifact changed after registration; refusing to seal it')
                if relative not in record['inventory']:
                    record['inventory'][relative] = current
        record['reserved'] = 0
        if persist:
            self.save()

    def abort(self, job):
        """Caller must first join any writer and prove that no live/candidate references exist."""
        if job in self.jobs:
            self.seal(job, check_limits=False, persist=False)
            return self.release(job)
        return {'job_id': job, 'bytes': 0, 'files': 0}

    def close(self):
        if self.closed:
            return
        for job in self.jobs.values():
            job['reserved'] = 0
        self.save()
        atomic_json(self.root / LEASE, {'owner': OWNER, 'pid': 0, 'start': '', 'reserved': {}, 'jobs': list(self.jobs)})
        self.closed = True

    def release_plan(self, job):
        if job not in self.jobs:
            return {'job_id': job, 'bytes': 0, 'files': 0}
        entry = self.jobs[job]
        if entry['reserved']:
            raise StoreError('PINNED', 'Owned job still has an active storage reservation')
        if any(not self.path(relative).is_relative_to(self.root / job) for relative in entry['inventory']):
            raise StoreError('INVALID_ARTIFACT', 'Job inventory refers outside its owned directory')
        files = scan(self.path(job, exists=True))
        for path in files:
            relative = str(path.relative_to(self.root))
            if relative not in entry['inventory'] or signature(path) != entry['inventory'][relative]:
                raise StoreError('UNMANAGED_FILES', 'Job contains an unowned or modified file; no files were removed')
        known = {self.root / job}
        known.update(self.root / path for path in entry.get('directories', []))
        for relative in entry['inventory']:
            known.update(path for path in (self.root / relative).parents if path.is_relative_to(self.root / job))
        for parent, directories, leaves in os.walk(self.root / job):
            if any((Path(parent) / name).is_symlink() for name in [*directories, *leaves]):
                raise StoreError('UNMANAGED_FILES', 'Job contains a symbolic link; no files were removed')
            if Path(parent) not in known:
                raise StoreError('UNMANAGED_FILES', 'Job contains an unowned directory; no files were removed')
        return {'job_id': job, 'bytes': sum(files.values()), 'files': len(files)}

    def release(self, job):
        with self.lock():
            if self.archived:
                self.require_inactive()
            result = self.release_plan(job)
            if job not in self.jobs:
                return result
            directory = self.path(job, exists=True)
            for relative in self.jobs[job]['inventory']:
                self.path(relative).unlink(missing_ok=True)
            for parent, directories, files in os.walk(directory, topdown=False):
                Path(parent).rmdir()
            self.records = {key: value for key, value in self.records.items() if value['record']['job_id'] != job}
            del self.jobs[job]
            self.save()
            return result
