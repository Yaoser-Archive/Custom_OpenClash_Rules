#!/usr/bin/env python3
"""Purge exact approved paths and verify bytes served at the client URL."""
import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import template_contract as contract


def refresh_file(path, expected_hash, branch='published', fetch=contract.fetch, sleep=time.sleep):
    if path not in {contract.TEMPLATE} | {'rule/' + n for n in contract.SYNC_RULES}:
        raise ValueError('unapproved purge path')
    key = contract.REPOSITORY + '@' + branch + '/' + path
    for attempt in range(5):
        try:
            result = json.loads(fetch('https://purge.jsdelivr.net/gh/' + key, attempts=1, timeout=10).decode('utf-8'))
            if not isinstance(result, dict) or result.get('status') != 'finished':
                raise ValueError('purge has not finished')
            # No cache-busting query: verify the same key terminals actually use.
            body = fetch('https://cdn.jsdelivr.net/gh/' + key, attempts=1, timeout=10)
            if contract.digest(body) == expected_hash:
                return
        except (OSError, RuntimeError, ValueError):
            pass
        if attempt < 4:
            sleep((2, 5, 10, 20)[attempt])
    raise RuntimeError('cache verification failed for ' + path)


def refresh(publication_sha=None):
    if publication_sha:
        if not contract.SHA.fullmatch(publication_sha):
            raise ValueError('invalid published revision')
        url = 'https://raw.githubusercontent.com/{}/{}/manifest.json'.format(contract.REPOSITORY, publication_sha)
    else:
        url = 'https://raw.githubusercontent.com/{}/published/manifest.json'.format(contract.REPOSITORY)
    manifest = contract.validate_manifest(json.loads(contract.fetch(url).decode('utf-8')))
    failures = []
    with ThreadPoolExecutor(max_workers=3) as workers:
        pending = {path: workers.submit(refresh_file, path, item['sha256'])
                   for path, item in sorted(manifest['files'].items())}
        for path, result in pending.items():
            try:
                result.result()
            except RuntimeError:
                failures.append(path)
    # Keep the previous public main-template URL usable for existing consumers.
    try:
        refresh_file(contract.TEMPLATE, manifest['files'][contract.TEMPLATE]['sha256'], branch='main')
    except RuntimeError:
        failures.append('main/' + contract.TEMPLATE)
    status = '缓存刷新失败，已验证规则仍已发布' if failures else '缓存刷新及文件哈希校验成功'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(status + '。\n')
            if failures:
                stream.write('未通过的缓存路径：' + ', '.join(failures) + '\n')
    if failures:
        raise RuntimeError(status)
    print(json.dumps({'cache_verified': True, 'source_sha': manifest['source_sha']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--publication-sha')
    args = parser.parse_args()
    refresh(args.publication_sha)
