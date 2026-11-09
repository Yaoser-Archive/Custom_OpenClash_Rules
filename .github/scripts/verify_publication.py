#!/usr/bin/env python3
"""Verify one published snapshot and its immutable client resources."""
import argparse
import email.utils
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import template_contract as contract


def retry_after_seconds(value, wall_clock=time.time):
    """Parse Retry-After seconds or an HTTP date."""
    if value is None:
        return None
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    try:
        date = email.utils.parsedate_to_datetime(value)
        return max(0, date.timestamp() - wall_clock())
    except (TypeError, ValueError, OverflowError):
        return None



def verify(publication_sha, fetch=contract.fetch, clock=time.monotonic, sleep=time.sleep, budget=240):
    if not contract.SHA.fullmatch(publication_sha):
        raise ValueError('invalid publication revision')
    deadline = clock() + budget
    results = []
    succeeded = False

    def download(url):
        transport_failures = 0
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('publication verification budget exhausted')
            try:
                body = fetch(url, timeout=min(10, remaining), attempts=1)
                if clock() >= deadline:
                    raise TimeoutError('publication verification budget exhausted')
                return body
            except (OSError, contract.ResourceFetchError) as exc:
                awaiting_visibility = getattr(exc, 'status', None) == 404
                if not awaiting_visibility:
                    transport_failures += 1
                    if transport_failures >= 3:
                        raise
                delay = retry_after_seconds(getattr(exc, 'retry_after', None))
                if delay is None:
                    delay = 5 if awaiting_visibility else 2 * transport_failures
                delay = max(1, delay)
                if clock() + delay >= deadline:
                    raise TimeoutError('publication verification budget exhausted')
                sleep(delay)

    def check(entry):
        label, url, expected = entry
        body = download(url)
        actual = contract.digest(body)
        if actual != expected:
            raise ValueError('SHA-256 mismatch for ' + label)
        return {'path': label, 'sha256': actual, 'verified': True}

    try:
        base = 'https://raw.githubusercontent.com/' + contract.REPOSITORY + '/'
        manifest = contract.validate_manifest(json.loads(download(base + publication_sha + '/manifest.json').decode('utf-8')))
        template = download(base + manifest['source_sha'] + '/' + contract.TEMPLATE)
        if contract.digest(template) != manifest['files'][contract.TEMPLATE]['sha256']:
            raise ValueError('published template SHA-256 mismatch')
        revision = contract.template_revision(template)
        if revision == 'published':
            raise ValueError('publication still uses mutable client resources')
        targets = [(path, base + manifest['source_sha'] + '/' + path, item['sha256'])
                   for path, item in sorted(manifest['files'].items())]
        targets += [('mirror/' + name, contract.rule_url(name, revision),
                     manifest['files']['rule/' + name]['sha256']) for name in contract.SYNC_RULES]
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [(entry[0], workers.submit(check, entry)) for entry in targets]
            for label, future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({'path': label, 'verified': False, 'error': str(exc)})
        if any(not item['verified'] for item in results):
            raise RuntimeError('immutable publication verification failed')
        succeeded = True
        return results
    except Exception as exc:
        results.append({'path': 'publication', 'verified': False, 'error': str(exc)})
        raise
    finally:
        report = {'publication_sha': publication_sha, 'published': True, 'verified': succeeded, 'results': results}
        print(json.dumps(report), flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write('\n固定版本资源校验结果\n\n```json\n' + json.dumps(report, ensure_ascii=False, indent=2) + '\n```\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--publication-sha', required=True)
    verify(parser.parse_args().publication_sha)
