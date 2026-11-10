#!/usr/bin/env python3
"""Poll public GitLab group hierarchy, direct members and onboarding projects.

Build a durable snapshot and append-only discovery history for the static website.
Standard-library only. Public groups/repos need no account; member lists may require a GitLab token.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
from pathlib import Path
import random
import sys
import time
from urllib import error, parse, request

BASE = 'https://gitlab.com/api/v4'
RECOVERABLE = {408, 425, 429, 500, 502, 503, 504}
MAX_HISTORY = 2500


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def unique(items, attr='id'):
    return {str(x[attr]): x for x in items}


class GitLab:
    def __init__(self, token=None, *, attempts=5, gap=0.5, sleeper=time.sleep):
        self.token = token
        self.attempts = attempts
        self.gap = gap
        self.sleeper = sleeper
        self.last_request = None

    def get(self, endpoint, **query):
        url = BASE + '/' + endpoint.lstrip('/')
        if query:
            url += '?' + parse.urlencode(query)
        for attempt in range(self.attempts):
            if self.last_request is not None:
                wait = self.gap - (time.monotonic() - self.last_request)
                if wait > 0:
                    self.sleeper(wait)
            self.last_request = time.monotonic()
            headers = {'User-Agent': 'AI-Village-Watchtower/1.0', 'Accept': 'application/json'}
            if self.token:
                headers['PRIVATE-TOKEN'] = self.token
            req = request.Request(url, headers=headers)
            try:
                with request.urlopen(req, timeout=40) as resp:
                    payload = json.load(resp)
                    next_page = resp.headers.get('X-Next-Page', '').strip()
                return payload, next_page
            except error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise PermissionError(f'GitLab HTTP {exc.code} at {endpoint}') from exc
                if exc.code not in RECOVERABLE or attempt == self.attempts - 1:
                    raise RuntimeError(f'GitLab HTTP {exc.code} at {endpoint}') from exc
                retry = exc.headers.get('Retry-After')
                try:
                    delay = float(retry)
                except (TypeError, ValueError):
                    try:
                        value = email.utils.parsedate_to_datetime(retry)
                        delay = max(0, (value - dt.datetime.now(dt.timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        delay = 2**attempt + random.random()
            except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt == self.attempts - 1:
                    raise RuntimeError(f'GitLab request failed at {endpoint}: {exc}') from exc
                delay = 2**attempt + random.random()
            self.sleeper(min(120, max(0, delay)))
        raise RuntimeError(f'Retries exhausted at {endpoint}')

    def all(self, endpoint, **query):
        """Use GitLab page headers, and fallback to full-page detection if absent."""
        rows = []
        page = 1
        while page <= 100:
            payload, next_page = self.get(endpoint, page=page, per_page=100, **query)
            if not isinstance(payload, list):
                raise ValueError(f'Expected list at {endpoint} page {page}')
            rows.extend(payload)
            if next_page:
                candidate = int(next_page)
                if candidate <= page:
                    raise RuntimeError(f'Invalid GitLab pagination at {endpoint}')
                page = candidate
            elif len(payload) == 100:
                page += 1
            else:
                return rows
        raise RuntimeError(f'Reached pagination limit at {endpoint}; refusing incomplete scan')


def scan(api, root_path='ai-village-agents', onboarding_path='ai-village-agents/onboarding'):
    root, _ = api.get('groups/' + parse.quote(root_path, safe=''))
    if not isinstance(root, dict) or not isinstance(root.get('id'), int):
        raise RuntimeError('Could not identify root GitLab group')
    descendants = api.all(f"groups/{root['id']}/descendant_groups")
    groups = {}
    for group in descendants:
        if not isinstance(group, dict) or not isinstance(group.get('id'), int):
            raise RuntimeError('Malformed subgroup in GitLab response')
        if group.get('visibility', 'public') != 'public':
            continue  # Never publish private subgroup data even with a token.
        path = group.get('full_path', '')
        if not path.startswith(root_path + '/'):
            raise RuntimeError('Unexpected group outside the monitored hierarchy')
        groups[str(group['id'])] = {
            'id': group['id'], 'name': group.get('name') or group.get('path') or 'Unnamed',
            'path': path, 'parent_id': group.get('parent_id'),
            'url': group.get('web_url') or 'https://gitlab.com/' + path,
            'created_at': group.get('created_at')}

    onboarding = next((g for g in groups.values() if g['path'] == onboarding_path), None)
    if onboarding is None:
        raise RuntimeError(f'Expected onboarding subgroup {onboarding_path!r} missing; scan aborted')
    memberships = {}
    membership_status = 'available'
    for group in sorted(groups.values(), key=lambda x: x['path']):
        try:
            members = api.all(f"groups/{group['id']}/members")  # DIRECT, not inherited
        except PermissionError:
            membership_status = 'authentication_required'
            memberships.clear()  # Never publish a partial list as complete.
            print('GitLab member lists require authentication; monitoring groups and repos only.',
                  file=sys.stderr)
            break
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get('id'), int):
                raise RuntimeError(f'Malformed member list for {group["path"]}')
            key = f'{group["id"]}:{member["id"]}'
            memberships[key] = {
                'key': key, 'group_id': group['id'], 'group_path': group['path'],
                'user_id': member['id'], 'name': member.get('name') or member.get('username') or 'Unknown',
                'username': member.get('username') or '',
                'access_level': member.get('access_level'),
                'url': member.get('web_url') or 'https://gitlab.com/' + (member.get('username') or '')}

    projects = api.all(f"groups/{onboarding['id']}/projects", include_subgroups='false',
                       with_shared='false')
    repos = {}
    for project in projects:
        if not isinstance(project, dict) or not isinstance(project.get('id'), int):
            raise RuntimeError('Malformed onboarding project in GitLab response')
        if project.get('visibility', 'public') != 'public':
            continue  # Only publish public projects.
        namespace = project.get('namespace') or {}
        full_path = project.get('path_with_namespace') or ''
        # Enforce exact direct-child namespace, even if GitLab changes endpoint defaults.
        if namespace.get('full_path') != onboarding_path or full_path.count('/') != onboarding_path.count('/') + 1:
            continue
        repos[str(project['id'])] = {
            'id': project['id'], 'name': project.get('name') or project.get('path') or 'Unnamed',
            'path': full_path, 'url': project.get('web_url') or 'https://gitlab.com/' + full_path,
            'created_at': project.get('created_at'), 'description': project.get('description') or ''}
    return {'root': root_path, 'onboarding': onboarding_path,
            'groups': groups, 'memberships': memberships, 'repos': repos,
            'membership_status': membership_status}


def compare(old, new, at):
    """Only report first-time additions relative to the immediately preceding scan."""
    changes = []
    for kind, source, label in [('group', 'groups', 'New subgroup'),
                                ('member', 'memberships', 'New subgroup member'),
                                ('repo', 'repos', 'New onboarding repository')]:
        earlier = old.get(source, {})
        current = new[source]
        if kind == 'member' and (old.get('membership_status') != 'available' or
                                 new.get('membership_status') != 'available'):
            continue
        for key in sorted(current.keys() - earlier.keys()):
            item = current[key]
            changes.append({'kind': kind, 'label': label, 'detected_at': at,
                            'entity_id': key, 'name': item['name'],
                            'path': item.get('group_path') or item.get('path'),
                            'url': item['url'],
                            'username': item.get('username') or None})
    order = {'group': 0, 'member': 1, 'repo': 2}
    return sorted(changes, key=lambda x: (order[x['kind']], x['path'], x['entity_id']))


def atomic_json(file, payload):
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(file.suffix + '.tmp')
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        tmp.replace(file)
    finally:
        tmp.unlink(missing_ok=True)


def load_json(path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding='utf-8'))


def run(api, output_dir, root='ai-village-agents', onboarding='ai-village-agents/onboarding', at=None):
    output_dir = Path(output_dir)
    state_path = output_dir / 'snapshot.json'
    history_path = output_dir / 'history.json'
    old = load_json(state_path, {})
    if old.get('status') == 'ready' and (old.get('root') != root or old.get('onboarding') != onboarding):
        raise ValueError('Monitored groups changed; clear the existing baseline explicitly first')
    history = load_json(history_path, {'events': []})
    if not isinstance(history.get('events'), list):
        raise ValueError('Invalid history file')
    # Fetch and validate everything BEFORE replacing any saved data.
    current = scan(api, root, onboarding)
    at = at or iso_now()
    initialized = old.get('status') == 'ready'
    if current['membership_status'] != 'available' and old.get('membership_status') == 'available':
        current['memberships'] = old['memberships']  # stale, never interpreted as zero
    discoveries = compare(old, current, at) if initialized else []
    state = {'status': 'ready', **current,
             'checked_at': at, 'baseline_at': old.get('baseline_at') or at,
             'last_discoveries': discoveries,
             'counts': {'groups': len(current['groups']),
                        'memberships': len(current['memberships']) if current['membership_status'] == 'available' else None,
                        'repos': len(current['repos']), 'new': len(discoveries)}}
    # Recover events if a previous run wrote snapshot.json but was interrupted
    # before its history.json update. Do not create duplicates on the next scan.
    previous = old.get('last_discoveries', []) if initialized else []
    existing_keys = {(e.get('kind'), e.get('entity_id'), e.get('detected_at'))
                     for e in history['events'] if isinstance(e, dict)}
    recovered = [e for e in previous if
                 (e.get('kind'), e.get('entity_id'), e.get('detected_at')) not in existing_keys]
    history['events'] = (discoveries + recovered + history['events'])[:MAX_HISTORY]
    history['updated_at'] = at
    # Snapshot first, history second; reconcile missing history on next scan.
    atomic_json(state_path, state)
    atomic_json(history_path, history)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='docs/data')
    parser.add_argument('--root', default='ai-village-agents')
    parser.add_argument('--onboarding', default='ai-village-agents/onboarding')
    args = parser.parse_args()
    api = GitLab(token=os.environ.get('GITLAB_TOKEN'))
    try:
        state = run(api, Path(args.output), args.root, args.onboarding)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print('Watchtower scan completed:', json.dumps(state['counts']),
          '| baseline:', state['baseline_at'], '| checked:', state['checked_at'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
