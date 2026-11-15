# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Read-only interruption diagnostics. Never replay operations or revive old scene handles."""

import json
from pathlib import Path

_terminal = {'succeeded', 'failed', 'cancelled', 'conflict', 'discarded', 'interrupted'}


def read(path, *, active_session=None, job_id=None):
    path = Path(path)
    size = path.stat().st_size
    if size > 17 * 1024 * 1024:
        raise ValueError('Journal exceeds the native session bound')
    jobs, session, cursor, closed, incomplete = {}, None, 0, False, False
    details = {}
    with path.open('rb') as stream:
        for line in stream:
            # A crash may leave the last write incomplete. Keep earlier valid records.
            if len(line) > 1024 * 1024 or not line.endswith(b'\n'):
                incomplete = True
                break
            try:
                event = json.loads(line)
                if not isinstance(event['cursor'], int) or event['cursor'] <= cursor:
                    raise ValueError('Non-increasing event cursor')
                if session is None:
                    session = event['session_id']
                if event['session_id'] != session:
                    raise ValueError('Mixed sessions')
                cursor = event['cursor']
                closed |= event['type'] == 'session.closed'
                if isinstance(event.get('job'), dict):
                    jobs[event['job']['job_id']] = event['job']
                    if job_id is not None and event['job']['job_id'] == job_id:
                        for key in ('accepted_parameters', 'outcome'):
                            if key in event:
                                details[key] = event[key]
            except (ValueError, TypeError, KeyError):
                incomplete = True
                break
    live = session is not None and session == active_session
    for job in jobs.values():
        if not live and job.get('state') not in _terminal:
            job['previous_state'] = job.get('state')
            job['state'] = 'interrupted'
            job['effects_unknown'] = job['previous_state'] in {'running', 'applying', 'discarding'}
    if job_id is not None:
        return {'session_id': session, 'active': live, 'closed': closed, 'incomplete_tail': incomplete,
                'job': jobs.get(job_id), **details, 'automatic_resume': False}
    return {'session_id': session, 'active': live, 'closed': closed, 'incomplete_tail': incomplete,
            'cursor': cursor, 'jobs': list(jobs.values()), 'automatic_resume': False}


def job(root, session_id, job_id, *, active_session):
    root = Path(root).resolve()
    if not isinstance(session_id, str) or len(session_id) != 64 or any(c not in '0123456789abcdef' for c in session_id):
        raise ValueError('History lookup requires a session ID')
    directory = root / session_id
    path = directory / 'journal.jsonl'
    if directory.is_symlink() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(directory):
        raise ValueError('History lookup is outside the owned session')
    return read(path, active_session=active_session, job_id=job_id)


def history(root, *, active_session, limit=8):
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    directories = [path for path in root.iterdir() if not path.is_symlink() and path.is_dir()
                   and len(path.name) == 64 and all(char in '0123456789abcdef' for char in path.name)]
    directories.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    results = []
    for directory in directories[:max(1, min(int(limit), 32))]:
        path = directory / 'journal.jsonl'
        if not path.is_file() or path.is_symlink():
            continue
        try:
            results.append(read(path, active_session=active_session))
        except (OSError, ValueError) as error:
            results.append({'session_id': directory.name, 'error': str(error), 'automatic_resume': False})
    return results
