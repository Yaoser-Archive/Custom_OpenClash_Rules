#!/usr/bin/env python3
"""Stage, validate, then publish; all branch writes are fast-forward only."""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

import template_contract as contract
import validate as validation


class PublicationRace(RuntimeError):
    pass


def git(root, *args, **kwargs):
    result = subprocess.run(['git'] + list(args), cwd=str(root), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=90, **kwargs)
    if result.returncode:
        raise RuntimeError('git {} failed (exit {})'.format(args[0], result.returncode))
    return result.stdout.decode().strip()


def remote_sha(root, branch):
    result = git(root, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch)
    return result.split()[0] if result else None


def resolve_upstream(root):
    result = git(root, 'ls-remote', 'https://github.com/' + contract.UPSTREAM + '.git', 'refs/heads/main')
    revision = result.split()[0]
    if not contract.SHA.fullmatch(revision):
        raise ValueError('invalid upstream revision')
    return revision


def stage_candidate(root, candidate, upstream_sha, fetch=contract.fetch):
    """Fetch every rule before replacing even one candidate file."""
    if not contract.SHA.fullmatch(upstream_sha):
        raise ValueError('upstream must be pinned')
    template = (root / contract.TEMPLATE).read_bytes()
    contract.validate_template(template)
    downloaded = {}
    for filename, behavior in contract.SYNC_RULES.items():
        url = 'https://raw.githubusercontent.com/{}/{}/rule/{}'.format(contract.UPSTREAM, upstream_sha, filename)
        body = fetch(url)
        contract.validate_rule(body, behavior)
        downloaded[filename] = body
    for filename, body in downloaded.items():
        path = candidate / 'rule' / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    target = candidate / contract.TEMPLATE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(template)


def publish_snapshot(root, candidate, source_sha, upstream_sha):
    """Use an isolated index: published contains only the tested allowlist."""
    manifest = contract.make_manifest(candidate, source_sha, upstream_sha)
    parent = remote_sha(root, 'published')
    if parent:
        git(root, 'fetch', '--no-tags', 'origin', parent)
    with tempfile.TemporaryDirectory(prefix='lite-index-') as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(pathlib.Path(tmp) / 'index'))
        git(root, 'read-tree', '--empty', env=env)
        for path in sorted(manifest['files']):
            blob = git(root, 'rev-parse', source_sha + ':' + path)
            # Confirm the tracked object is exactly the candidate just tested.
            body = subprocess.check_output(['git', 'show', source_sha + ':' + path], cwd=str(root))
            if contract.digest(body) != manifest['files'][path]['sha256']:
                raise ValueError('published source does not match tested candidate')
            git(root, 'update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + path, env=env)
        content = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        blob = git(root, 'hash-object', '-w', '--stdin', input=content)
        git(root, 'update-index', '--add', '--cacheinfo', '100644,' + blob + ',manifest.json', env=env)
        tree = git(root, 'write-tree', env=env)
        if parent and tree == git(root, 'rev-parse', parent + '^{tree}'):
            return parent, manifest
        args = ['commit-tree', tree]
        if parent:
            args += ['-p', parent]
        commit = git(root, *args, input=('Chore: 发布已验证的 Lite 模板与规则\n\n'
                     '源版本: {}\n上游版本: {}\n验证: 模板契约、规则内容及固定 Mihomo 核心。\n'
                     .format(source_sha, upstream_sha)).encode('utf-8'))
        # A competing publication rejects this push; never replace its history.
        try:
            git(root, 'push', 'origin', commit + ':refs/heads/published')
        except RuntimeError:
            if remote_sha(root, 'published') != parent:
                raise PublicationRace('published advanced before push')
            raise
        if remote_sha(root, 'published') != commit:
            raise PublicationRace('published advanced after push')
        return commit, manifest


def output(name, value):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as stream:
            stream.write('{}={}\n'.format(name, value))


def publish(root, candidate, core, upstream_sha=None):
    # reset --hard below is restricted to the disposable Actions checkout.
    if os.environ.get('GITHUB_ACTIONS') != 'true' or root.resolve() != pathlib.Path(os.environ.get('GITHUB_WORKSPACE', '')).resolve():
        raise RuntimeError('branch publication is only allowed in its Actions checkout')
    upstream_sha = upstream_sha or resolve_upstream(root)
    script_hashes = {p.relative_to(root).as_posix(): contract.digest(p.read_bytes())
                     for p in (root / '.github').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    for attempt in range(3):
        git(root, 'fetch', '--no-tags', 'origin', 'main')
        git(root, 'reset', '--hard', 'origin/main')
        if any(contract.digest((root / p).read_bytes()) != h for p, h in script_hashes.items()):
            raise RuntimeError('publisher code changed during this run; rerun with the new code')
        base_sha = git(root, 'rev-parse', 'HEAD')
        stage_candidate(root, candidate, upstream_sha)
        validation.validate(candidate, '/base/config/lite-ci/' + contract.TEMPLATE, core)
        paths = ['rule/' + n for n in contract.SYNC_RULES]
        for path in paths:
            shutil.copyfile(str(candidate / path), str(root / path))
        git(root, 'add', '--', *paths)
        if git(root, 'diff', '--cached', '--name-only'):
            git(root, 'commit', '-m', 'Chore: 同步已验证的上游规则',
                '-m', '锁定同一上游提交 {}，通过规则与完整模板检查后更新。'.format(upstream_sha))
        source_sha = git(root, 'rev-parse', 'HEAD')
        try:
            if source_sha != base_sha:
                try:
                    git(root, 'push', 'origin', 'HEAD:main')
                except RuntimeError:
                    if remote_sha(root, 'main') != base_sha:
                        raise PublicationRace('main advanced before push')
                    raise
            if remote_sha(root, 'main') != source_sha:
                raise PublicationRace('main advanced during publication')
            publication_sha, manifest = publish_snapshot(root, candidate, source_sha, upstream_sha)
            output('publication_sha', publication_sha)
            output('source_sha', source_sha)
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            if summary:
                with open(summary, 'a', encoding='utf-8') as stream:
                    stream.write('规则发布成功：{}；上游：{}。缓存状态由后续步骤单独报告。\n'.format(publication_sha, upstream_sha))
            print(json.dumps({'publication_sha': publication_sha, 'source_sha': source_sha,
                              'upstream_sha': upstream_sha, 'published': True}))
            return manifest
        except PublicationRace:
            # Retry only a genuine competing branch update, not auth/network errors.
            if attempt == 2:
                raise
    raise RuntimeError('publication attempts exhausted')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=pathlib.Path, required=True)
    parser.add_argument('--candidate', type=pathlib.Path, required=True)
    parser.add_argument('--core', type=pathlib.Path, required=True)
    args = parser.parse_args()
    publish(args.root.resolve(), args.candidate.resolve(), args.core.resolve())
