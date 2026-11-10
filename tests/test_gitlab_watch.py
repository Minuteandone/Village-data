"""Offline tests for nested membership tracking, baseline and onboarding scope."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'gitlab_watch.py'
spec = importlib.util.spec_from_file_location('gitlab_watch', MODULE)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

ROOT = {'id': 100, 'full_path': 'ai-village-agents'}


def group(id, path, parent=100):
    return {'id': id, 'name': path.split('/')[-1], 'full_path': path,
            'web_url': f'https://gitlab.com/{path}', 'parent_id': parent}


def member(id, name):
    return {'id': id, 'name': name, 'username': name.lower(),
            'web_url': f'https://gitlab.com/{name.lower()}'}


def project(id, path):
    return {'id': id, 'name': path.split('/')[-1], 'path_with_namespace': path,
            'namespace': {'full_path': '/'.join(path.split('/')[:-1])},
            'web_url': f'https://gitlab.com/{path}'}


class FakeApi:
    def __init__(self, extra_groups=False, new_member=False, repos=None, fail=False):
        self.extra_groups = extra_groups
        self.new_member = new_member
        self.repos = repos or []
        self.fail = fail
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return ROOT, ''

    def all(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.fail and 'members' in path:
            raise RuntimeError('temporary error')
        if path.endswith('descendant_groups'):
            result = [group(101, 'ai-village-agents/onboarding'),
                      group(102, 'ai-village-agents/research')]
            if self.extra_groups:
                result.append(group(103, 'ai-village-agents/research/secret-garden', 102))
            return result
        if path.endswith('/101/members'):
            return [member(4, 'Claude')]
        if path.endswith('/102/members'):
            return [member(8, 'Gemini')] + ([member(9, 'NewAgent')] if self.new_member else [])
        if path.endswith('/103/members'):
            return [member(10, 'TreeAgent')]
        if path.endswith('/101/projects'):
            return self.repos
        raise AssertionError(path)


class WatchTests(unittest.TestCase):
    def test_baseline_and_nested_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = watch.run(FakeApi(repos=[project(12, 'ai-village-agents/onboarding/first')]),
                              tmp, at='2026-11-10T14:00:00Z')
            self.assertEqual(first['counts']['new'], 0)
            self.assertEqual(first['counts']['groups'], 2)
            second = watch.run(FakeApi(extra_groups=True, new_member=True,
                          repos=[project(12, 'ai-village-agents/onboarding/first'),
                                 project(13, 'ai-village-agents/onboarding/second'),
                                 project(14, 'ai-village-agents/other/not-onboarding'),
                                 project(15, 'ai-village-agents/onboarding/sub/nested')]),
                               tmp, at='2026-11-10T16:00:00Z')
            self.assertEqual(second['counts']['groups'], 3)
            self.assertEqual(second['counts']['repos'], 2)
            self.assertEqual(second['counts']['new'], 4) # nested group, two memberships, repo
            kinds = [x['kind'] for x in second['last_discoveries']]
            self.assertEqual(kinds, ['group', 'member', 'member', 'repo'])
            history = json.loads((Path(tmp) / 'history.json').read_text())
            self.assertEqual(len(history['events']), 4)
            third = watch.run(FakeApi(extra_groups=True, new_member=True,
                    repos=[project(12, 'ai-village-agents/onboarding/first'),
                           project(13, 'ai-village-agents/onboarding/second')]), tmp,
                    at='2026-11-10T18:00:00Z')
            self.assertEqual(third['counts']['new'], 0)
            self.assertEqual(len(json.loads((Path(tmp) / 'history.json').read_text())['events']), 4)

    def test_fail_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch.run(FakeApi(), tmp, at='2026-11-10T14:00:00Z')
            previous = (Path(tmp) / 'snapshot.json').read_bytes()
            with self.assertRaises(RuntimeError):
                watch.run(FakeApi(fail=True), tmp, at='2026-11-10T16:00:00Z')
            self.assertEqual(previous, (Path(tmp) / 'snapshot.json').read_bytes())

    def test_missing_onboarding_fails(self):
        class NoOnboarding(FakeApi):
            def all(self, path, **kwargs):
                if path.endswith('descendant_groups'):
                    return [group(102, 'ai-village-agents/research')]
                return super().all(path, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'onboarding subgroup'):
                watch.run(NoOnboarding(), tmp)

    def test_public_discovery_stays_unauthenticated_with_token(self):
        # An authenticated GitLab descendant listing may exclude public groups
        # unless the token's owner belongs to them. Never call it for discovery.
        class TokenMemberApi(FakeApi):
            def get(self, path, **kwargs):
                raise AssertionError('Token must not request group metadata')

            def all(self, path, **kwargs):
                if not path.endswith('/members'):
                    raise AssertionError('Token must not list groups or projects')
                return super().all(path, **kwargs)

        public = FakeApi(repos=[project(12, 'ai-village-agents/onboarding/first')])
        private_members = TokenMemberApi()
        with tempfile.TemporaryDirectory() as tmp:
            state = watch.run(public, tmp, at='2026-11-10T16:00:00Z',
                              member_api=private_members)
        self.assertEqual(state['counts']['groups'], 2)
        self.assertEqual(state['counts']['repos'], 1)
        self.assertEqual(state['counts']['memberships'], 2)
        self.assertTrue(any(path.endswith('descendant_groups') for path, _ in public.calls))
        self.assertTrue(all(not path.endswith('/members') for path, _ in public.calls))
        self.assertTrue(all(path.endswith('/members') for path, _ in private_members.calls))

    def test_token_without_member_access_keeps_groups_and_repos(self):
        class DeniedMembers(FakeApi):
            def get(self, path, **kwargs):
                raise AssertionError('Token may only fetch members')

            def all(self, path, **kwargs):
                if not path.endswith('/members'):
                    raise AssertionError('Token may only fetch members')
                raise PermissionError('GitLab HTTP 403')

        public = FakeApi(repos=[project(12, 'ai-village-agents/onboarding/first')])
        with tempfile.TemporaryDirectory() as tmp:
            state = watch.run(public, tmp, at='2026-11-10T16:00:00Z',
                              member_api=DeniedMembers())
        self.assertEqual(state['counts']['groups'], 2)
        self.assertEqual(state['counts']['repos'], 1)
        self.assertEqual(state['membership_status'], 'authentication_required')
        self.assertIsNone(state['counts']['memberships'])

    def test_paginated_fetch(self):
        class Paging(watch.GitLab):
            def __init__(self):
                self.calls = []
            def get(self, endpoint, **kwargs):
                self.calls.append(kwargs['page'])
                if kwargs['page'] == 1:
                    return [1], '2'
                return [2], ''
        api = Paging()
        self.assertEqual(api.all('groups/100/members'), [1, 2])
        self.assertEqual(api.calls, [1, 2])

    def test_membership_auth_required_keeps_public_scans(self):
        class NoMemberAccess(FakeApi):
            def all(self, path, **kwargs):
                if path.endswith('/members'):
                    raise PermissionError('GitLab HTTP 401')
                return super().all(path, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            report = watch.run(
                NoMemberAccess(repos=[project(22, 'ai-village-agents/onboarding/new-agent')]),
                tmp, at='2026-11-10T14:00:00Z')
            self.assertEqual(report['membership_status'], 'authentication_required')
            self.assertIsNone(report['counts']['memberships'])
            self.assertEqual(report['counts']['groups'], 2)
            self.assertEqual(report['counts']['repos'], 1)
            self.assertTrue(report['status'] == 'ready')

    def test_scope_check(self):
        old = {'groups': {}, 'memberships': {}, 'repos': {}}
        new = {'groups': {'12': {'name': 'sub', 'path': 'ai-village-agents/sub', 'url': 'https://gitlab.com/g'}},
               'memberships': {}, 'repos': {}}
        self.assertEqual(watch.compare(old, new, '2026-11-10')[0]['kind'], 'group')


if __name__ == '__main__':
    unittest.main()
