import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import publication
import template_contract as contract
import verify_publication

ROOT = pathlib.Path(__file__).resolve().parents[2]


class VersionedTests(unittest.TestCase):
    def test_both_contracts_and_rejected_references(self):
        body = (ROOT / contract.TEMPLATE).read_bytes()
        modern = contract.render_template(body, 'a' * 40)
        legacy = modern.decode()
        for name in contract.RULES:
            legacy = legacy.replace(contract.rule_url(name, 'a' * 40), contract.rule_url(name))
        self.assertEqual(contract.template_revision(legacy.encode()), 'published')
        self.assertEqual(contract.template_revision(modern), 'a' * 40)
        for old, new in [('a' * 40, 'a' * 7), ('cdn.jsdmirror.cn', 'example.invalid'),
                         (contract.REPOSITORY, 'other/repository'), ('@' + 'a' * 40, '@main')]:
            with self.subTest(new=new), self.assertRaises(ValueError):
                contract.validate_template(modern.decode().replace(old, new).encode())
        mixed = modern.decode().replace(contract.rule_url('Custom_Direct_Domain.yaml', 'a' * 40),
                                         contract.rule_url('Custom_Direct_Domain.yaml', 'b' * 40))
        with self.assertRaises(ValueError):
            contract.validate_template(mixed.encode())

    def test_converted_policy_checks_one_revision(self):
        from test_routing_contract import converted_fixture
        policy, names = converted_fixture()
        for provider in policy['rule-providers'].values():
            provider['url'] = contract.rule_url(provider['url'].rsplit('/', 1)[-1], 'a' * 40)
        contract.validate_policy(policy, names)
        next(iter(policy['rule-providers'].values()))['url'] = contract.rule_url('Custom_Direct_Domain.yaml', 'b' * 40)
        with self.assertRaises(ValueError):
            contract.validate_policy(policy, names)

    def test_verification_checks_raw_and_mirror_hashes(self):
        template = contract.render_template((ROOT / contract.TEMPLATE).read_bytes(), 'a' * 40)
        manifest = contract.make_manifest(ROOT, 'b' * 40, 'c' * 40)
        manifest['files'][contract.TEMPLATE]['sha256'] = contract.digest(template)
        for corrupt in (False, True):
            urls = []
            def fetch(url, **kwargs):
                urls.append(url)
                self.assertLessEqual(kwargs['timeout'], 10)
                if url.endswith('/manifest.json'):
                    return json.dumps(manifest).encode()
                if url.endswith(contract.TEMPLATE):
                    return template
                if corrupt and 'jsdmirror' in url:
                    return b'wrong'
                return (ROOT / 'rule' / url.rsplit('/', 1)[-1]).read_bytes()
            with mock.patch('sys.stdout', io.StringIO()):
                if corrupt:
                    with self.assertRaises(RuntimeError):
                        verify_publication.verify('d' * 40, fetch=fetch)
                else:
                    results = verify_publication.verify('d' * 40, fetch=fetch)
                    self.assertEqual(len(results), 17)
            self.assertTrue(all('@published' not in url and 'purge.' not in url for url in urls))

    def test_global_deadline_includes_manifest_request(self):
        timer = [0]
        def fetch(url, **kwargs):
            timer[0] += 241
            return b'{}'
        with mock.patch('sys.stdout', io.StringIO()), self.assertRaises(TimeoutError):
            verify_publication.verify('a' * 40, fetch=fetch, clock=lambda: timer[0])

    def test_retry_after_cannot_exceed_global_budget(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            raise contract.ResourceFetchError('limited', status=429, retry_after='300')
        with mock.patch('sys.stdout', io.StringIO()), self.assertRaises(TimeoutError):
            verify_publication.verify('a' * 40, fetch=fetch)
        self.assertEqual(len(calls), 1)

    def fixture(self, directory):
        base = pathlib.Path(directory)
        remote, local = base / 'remote.git', base / 'checkout'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        subprocess.run(['git', 'clone', str(remote), str(local)], check=True, capture_output=True)
        publication.git(local, 'checkout', '-b', 'main')
        publication.git(local, 'config', 'user.name', 'Fixture')
        publication.git(local, 'config', 'user.email', 'fixture@example.invalid')
        publication.git(local, 'config', 'core.autocrlf', 'false')
        for path in [contract.TEMPLATE] + ['rule/' + name for name in contract.SYNC_RULES]:
            target = local / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(ROOT / path), str(target))
        template = local / contract.TEMPLATE
        body = template.read_text(encoding='utf-8')
        revision = contract.template_revision(body.encode())
        for name in contract.RULES:
            body = body.replace(contract.rule_url(name, revision), contract.rule_url(name))
        template.write_text(body, encoding='utf-8')
        publication.git(local, 'add', '.')
        publication.git(local, 'commit', '-m', 'fixture')
        publication.git(local, 'push', 'origin', 'main')
        return remote, local

    def run_publish(self, local, mutate=False, invalid=False):
        candidate = local.parent / 'candidate'
        def stage(root, target, upstream):
            shutil.copytree(str(root / 'cfg'), str(target / 'cfg'), dirs_exist_ok=True)
            shutil.copytree(str(root / 'rule'), str(target / 'rule'), dirs_exist_ok=True)
            if mutate:
                (target / 'rule/Custom_Direct_Domain.yaml').write_bytes(b'payload: [changed.example]')
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_WORKSPACE': str(local)}
        with mock.patch.dict(os.environ, env), mock.patch.object(publication, 'stage_candidate', side_effect=stage), \
             mock.patch.object(publication.validation, 'validate', side_effect=ValueError('invalid') if invalid else None), \
             mock.patch('sys.stdout', io.StringIO()):
            return publication.publish(local, candidate, local / 'mihomo', 'c' * 40)

    def test_migration_reuse_rule_update_and_template_update(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, local = self.fixture(directory)
            first = self.run_publish(local)
            revision = contract.template_revision((local / contract.TEMPLATE).read_bytes())
            self.assertNotEqual(revision, 'published')
            self.assertEqual(self.run_publish(local), first)
            second = self.run_publish(local, mutate=True)
            new_revision = contract.template_revision((local / contract.TEMPLATE).read_bytes())
            self.assertNotEqual(new_revision, revision)
            publication.verify_rule_revision(local, local, new_revision)
            self.assertEqual(publication.remote_sha(local, 'main'), second['source_sha'])
            path = local / contract.TEMPLATE
            path.write_bytes(b'; template edit\n' + path.read_bytes())
            publication.git(local, 'add', contract.TEMPLATE)
            publication.git(local, 'commit', '-m', 'edit template')
            publication.git(local, 'push', 'origin', 'main')
            third = self.run_publish(local)
            self.assertNotEqual(third['source_sha'], second['source_sha'])
            self.assertEqual(contract.template_revision(path.read_bytes()), new_revision)
            before = publication.remote_sha(local, 'main')
            with self.assertRaises(ValueError):
                self.run_publish(local, mutate=True, invalid=True)
            self.assertEqual(publication.remote_sha(local, 'main'), before)

    def test_rule_only_source_commit_repairs_old_template_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, local = self.fixture(directory)
            self.run_publish(local)
            path = local / 'rule/Custom_Direct_Domain.yaml'
            path.write_bytes(b'payload: [manual.example]')
            publication.git(local, 'add', 'rule/Custom_Direct_Domain.yaml')
            publication.git(local, 'commit', '-m', 'manual rule update')
            rule_commit = publication.git(local, 'rev-parse', 'HEAD')
            publication.git(local, 'push', 'origin', 'main')
            self.run_publish(local)
            self.assertEqual(contract.template_revision((local / contract.TEMPLATE).read_bytes()), rule_commit)
            publication.verify_rule_revision(local, local, rule_commit)

    def test_atomic_push_rejection_preserves_both_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, local = self.fixture(directory)
            self.run_publish(local)
            before = {branch: publication.remote_sha(local, branch) for branch in ('main', 'published')}
            hook = remote / 'hooks/pre-receive'
            hook.write_text('#!/bin/sh\nwhile read old new ref; do\n if [ "$ref" = refs/heads/published ]; then exit 1; fi\ndone\nexit 0\n')
            hook.chmod(0o700)
            with self.assertRaises(RuntimeError):
                self.run_publish(local, mutate=True)
            self.assertEqual({branch: publication.remote_sha(local, branch) for branch in before}, before)
