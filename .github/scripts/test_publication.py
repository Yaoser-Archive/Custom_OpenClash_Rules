import copy
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

import cache_refresh
import publication
import template_contract as contract

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Response(io.BytesIO):
    def getcode(self):
        return 200


class ResourceTests(unittest.TestCase):
    def test_http_error_html_and_oversize_are_rejected(self):
        def fail(*args, **kwargs):
            raise urllib.error.HTTPError('https://example.invalid', 404, 'Not Found', {}, None)
        with self.assertRaises(RuntimeError):
            contract.fetch('https://example.invalid', opener=fail, sleep=lambda _: None)
        with self.assertRaises(RuntimeError):
            contract.fetch('https://example.invalid', opener=lambda *a, **kw: Response(b'x' * (contract.MAX_BYTES + 1)), attempts=1)
        for body in (b'<html>upstream error</html>', b'payload: []', b'payload: [true]', b'payload: [bad,'):
            with self.assertRaises((ValueError, contract.yaml.YAMLError)):
                contract.validate_rule(body, 'domain')

    def test_ip_rules_keep_no_resolve_and_reject_policy_injection(self):
        contract.validate_rule(b'payload: ["IP-CIDR,192.0.2.0/24,no-resolve"]', 'classical')
        for entry in ('IP-CIDR,192.0.2.0/24', 'IP-CIDR,192.0.2.0/24,DIRECT', 'DST-PORT,443,DIRECT'):
            with self.assertRaises(ValueError):
                contract.validate_rule(('payload: ["' + entry + '"]').encode(), 'classical')

    def test_template_contract_and_google_always_uses_foreign_proxy(self):
        body = (ROOT / contract.TEMPLATE).read_bytes()
        contract.validate_template(body)
        text = body.decode()
        for old, new in [
            ('ruleset=谷歌服务,[]GEOSITE,google-cn', 'ruleset=全球直连,[]GEOSITE,google-cn'),
            ('GEOIP,cn,no-resolve', 'GEOIP,cn'),
            ('@published/rule/', '@main/rule/'),
            (',1800', ',28800'),
            ('ruleset=漏网之鱼,[]FINAL', 'ruleset=全球直连,[]FINAL'),
        ]:
            with self.assertRaises(ValueError):
                contract.validate_template(text.replace(old, new).encode())
        group = 'custom_proxy_group=手动选择' + chr(96) + 'select' + chr(96)
        with self.assertRaises(ValueError):
            contract.validate_template(text.replace(group + '[]自动选择', group + '[]全球直连').encode())

    def test_google_must_precede_direct_overrides_and_cannot_offer_direct(self):
        body = (ROOT / contract.TEMPLATE).read_text(encoding='utf-8')
        google = 'ruleset=谷歌服务,[]GEOSITE,google-cn'
        game = 'ruleset=全球直连,[]GEOSITE,category-games@cn'
        reordered = body.replace(google + '\n', '').replace(game, game + '\n' + google)
        group = 'custom_proxy_group=谷歌服务' + chr(96) + 'select' + chr(96) + '[]手动选择'
        bad_group = body.replace(group, group + chr(96) + '[]全球直连')
        for bad in (reordered, bad_group):
            with self.assertRaises(ValueError):
                contract.validate_template(bad.encode())

    def test_failed_download_cannot_replace_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = pathlib.Path(tmp)
            preserved = candidate / 'rule/Custom_Direct_Domain.yaml'
            preserved.parent.mkdir()
            preserved.write_bytes(b'previous-valid-resource')
            calls = []
            def fetch(url):
                calls.append(url)
                if len(calls) == 3:
                    raise RuntimeError('404')
                return (ROOT / 'rule' / url.rsplit('/', 1)[-1]).read_bytes()
            with self.assertRaises(RuntimeError):
                publication.stage_candidate(ROOT, candidate, 'a' * 40, fetch)
            self.assertEqual(preserved.read_bytes(), b'previous-valid-resource')
            self.assertTrue(all('/' + 'a' * 40 + '/rule/' in url for url in calls))

    def test_manifest_rejects_missing_hash_and_unknown_paths(self):
        value = contract.make_manifest(ROOT, 'a' * 40, 'b' * 40)
        for mutate in (lambda v: v['files'].pop(contract.TEMPLATE),
                       lambda v: v['files'][contract.TEMPLATE].update(sha256=''),
                       lambda v: v['files'].update({'../settings.json': {'sha256': '0' * 64, 'behavior': 'ini'}})):
            bad = copy.deepcopy(value); mutate(bad)
            with self.assertRaises(ValueError):
                contract.validate_manifest(bad)


class CacheTests(unittest.TestCase):
    def test_retry_logs_distinguish_stages_and_keep_last_failure(self):
        from test_cache_refresh import FakeClock, finished
        failures = [('purge_request', contract.ResourceFetchError('HTTP 429', status=429)),
                    ('purge_status', b'{}'),
                    ('cdn_request', TimeoutError('timed out')),
                    ('cdn_digest', b'stale')]
        for stage, failure in failures:
            with self.subTest(stage=stage):
                timer = FakeClock()
                def fetch(url, **kwargs):
                    if 'purge.jsdelivr.net' in url:
                        if stage == 'purge_request':
                            raise failure
                        if stage == 'purge_status':
                            return failure if '/status/' in url else b'{"id":"task-1","status":"pending"}'
                        return finished()
                    if stage == 'cdn_request':
                        raise failure
                    return b'stale'
                output = io.StringIO()
                with mock.patch('sys.stdout', output), self.assertRaisesRegex(RuntimeError, stage):
                    cache_refresh.refresh_file(contract.TEMPLATE, '0' * 64, fetch=fetch,
                                               sleep=timer.sleep, clock=timer.now, budget=15)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                failures_for_stage = [e for e in events if e['event'] == 'cache_retry_failed' and e['stage'] == stage]
                self.assertTrue(failures_for_stage)
                self.assertTrue(all(e['attempt'] > 0 and e['branch'] == 'published' for e in failures_for_stage))
                self.assertTrue(all(e['path'] == contract.TEMPLATE for e in events))
                if stage == 'cdn_digest':
                    self.assertIn(contract.digest(b'stale'), failures_for_stage[-1]['error'])

    def test_fetch_preserves_http_status_and_timeout_reason(self):
        for error, expected in [(urllib.error.HTTPError('https://example.invalid', 429, 'rate limited', {}, None), 'HTTP 429'),
                                (TimeoutError('timed out'), 'timed out')]:
            with mock.patch.object(contract.urllib.request, 'urlopen'):
                def fail(*args, **kwargs):
                    raise error
                with self.assertRaisesRegex(RuntimeError, expected):
                    contract.fetch('https://example.invalid', opener=fail, attempts=1)

    def test_async_purge_and_stale_cache_are_retried(self):
        from test_cache_refresh import FakeClock, finished
        body = b'approved'
        timer = FakeClock()
        replies = iter([b'stale', b'{"id":"task-1","status":"pending"}',
                        finished(), body])
        requests = []
        def fetch(url, **kwargs):
            requests.append(url)
            return next(replies)
        cache_refresh.refresh_file(contract.TEMPLATE, contract.digest(body), fetch=fetch,
                                   sleep=timer.sleep, clock=timer.now)
        self.assertEqual(len(requests), 4)
        self.assertEqual(len([url for url in requests if 'purge.jsdelivr.net/gh/' in url]), 1)
        self.assertIn('/status/task-1', requests[2])
        self.assertTrue(all('?' not in url for url in requests))

    def test_finished_status_does_not_hide_wrong_bytes(self):
        from test_cache_refresh import FakeClock, finished
        timer = FakeClock()
        def fetch(url, **kwargs):
            return finished() if 'purge.jsdelivr.net' in url else b'stale'
        with self.assertRaises(RuntimeError):
            cache_refresh.refresh_file(contract.TEMPLATE, '0' * 64, fetch=fetch,
                                       sleep=timer.sleep, clock=timer.now, budget=15)


class PublicationTests(unittest.TestCase):
    def test_competing_main_updates_rebuild_and_revalidate_at_most_three_times(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_WORKSPACE': str(ROOT)}
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted), mock.patch.dict(publication.os.environ, env, clear=True), \
                 mock.patch.object(publication, 'git', return_value='a' * 40), \
                 mock.patch.object(publication.shutil, 'copyfile'), \
                 mock.patch.object(publication, 'stage_candidate') as stage, \
                 mock.patch.object(publication.validation, 'validate') as validate, \
                 mock.patch.object(publication, 'remote_sha', side_effect=['b' * 40, 'b' * 40, ('b' if exhausted else 'a') * 40]), \
                 mock.patch.object(publication, 'publish_snapshot', return_value=('c' * 40, {'files': {}})):
                if exhausted:
                    with self.assertRaises(publication.PublicationRace):
                        publication.publish(ROOT, ROOT, ROOT / 'mihomo', 'd' * 40)
                else:
                    self.assertEqual(publication.publish(ROOT, ROOT, ROOT / 'mihomo', 'd' * 40), {'files': {}})
                self.assertEqual(stage.call_count, 3)
                self.assertEqual(validate.call_count, 3)
                self.assertTrue(all(call[0][2] == 'd' * 40 for call in stage.call_args_list))

    def test_local_invocation_cannot_reset_a_checkout(self):
        with mock.patch.dict(publication.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                publication.publish(ROOT, ROOT, ROOT / 'mihomo')

    def test_published_branch_contains_only_checked_snapshot_and_preserves_history(self):
        cache = ROOT / '.cache'
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='git-fixture-', dir=str(cache)) as tmp:
            base = pathlib.Path(tmp)
            remote, local = base / 'remote.git', base / 'checkout'
            subprocess.run(['git', 'init', '--bare', str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(['git', 'clone', str(remote), str(local)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            publication.git(local, 'config', 'user.name', 'Fixture')
            publication.git(local, 'config', 'user.email', 'fixture@example.invalid')
            publication.git(local, 'config', 'core.autocrlf', 'false')
            for path in [contract.TEMPLATE] + ['rule/' + n for n in contract.SYNC_RULES]:
                target = local / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / path).read_bytes())
            (local / 'private-input.json').write_text('do not publish')
            publication.git(local, 'add', '.')
            publication.git(local, 'commit', '-m', 'fixture')
            source = publication.git(local, 'rev-parse', 'HEAD')
            first, manifest = publication.publish_snapshot(local, local, source, 'a' * 40)
            files = set(publication.git(local, 'ls-tree', '-r', '--name-only', first).splitlines())
            self.assertEqual(files, set(manifest['files']) | {'manifest.json'})
            self.assertNotIn('private-input.json', files)
            again, _ = publication.publish_snapshot(local, local, source, 'a' * 40)
            self.assertEqual(first, again)
            second, _ = publication.publish_snapshot(local, local, source, 'b' * 40)
            self.assertEqual(publication.git(local, 'rev-parse', second + '^'), first)

    def test_racing_publication_rejects_competing_update(self):
        with mock.patch.object(publication, 'remote_sha', side_effect=['a' * 40, 'b' * 40]), \
             mock.patch.object(publication, 'git') as git, \
             mock.patch.object(publication.contract, 'make_manifest', return_value={'files': {}}), \
             mock.patch.object(publication.subprocess, 'check_output'):
            def command(root, *args, **kwargs):
                if args[0] == 'push':
                    raise RuntimeError('non-fast-forward')
                if args[0] == 'rev-parse':
                    return 'old-tree'
                return 'new-tree'
            git.side_effect = command
            with self.assertRaises(publication.PublicationRace):
                publication.publish_snapshot(ROOT, ROOT, 'c' * 40, 'd' * 40)


if __name__ == '__main__':
    unittest.main()
