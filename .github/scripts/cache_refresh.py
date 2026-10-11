#!/usr/bin/env python3
"""Verify client bytes first, then purge stale mutable URLs within a fixed budget."""
import argparse
import email.utils
import json
import os
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import template_contract as contract


FILE_BUDGET = 120
HTTP_TIMEOUT = 10
MAX_WORKERS = 3
BACKOFF = (2, 5, 10, 20)


def retry_after_seconds(value, wall_clock=time.time):
    """Accept Retry-After seconds or an HTTP date; malformed headers are ignored."""
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


def expected_errors():
    # Do not treat arbitrary RuntimeError/TypeError as recoverable network errors.
    error = getattr(contract, 'ResourceFetchError', None)
    return (OSError, urllib.error.URLError) + ((error,) if error else ())


class CacheVerificationError(RuntimeError):
    pass


class FileRefresh:
    def __init__(self, path, expected_hash, branch, fetch, sleep, clock, wall_clock, budget):
        self.path, self.expected_hash, self.branch = path, expected_hash, branch
        self.fetch, self.sleep, self.clock, self.wall_clock = fetch, sleep, clock, wall_clock
        self.deadline = clock() + budget
        self.key = contract.REPOSITORY + '@' + branch + '/' + path
        self.cdn_url = 'https://cdn.jsdelivr.net/gh/' + self.key
        self.attempts, self.errors, self.cooldowns = {}, {}, {}
        self.purge_started, self.purge_id, self.purge_finished = False, None, False
        self.purge_status, self.warnings = None, []

    def log(self, event, stage, **details):
        value = {'event': event, 'path': self.path, 'branch': self.branch,
                 'stage': stage, 'attempt': self.attempts.get(stage, 0)}
        value.update(details)
        print(json.dumps(value, ensure_ascii=True), flush=True)

    def failure(self, stage, exc):
        self.errors[stage] = str(exc)
        status = getattr(exc, 'status', getattr(exc, 'code', None))
        headers = getattr(exc, 'headers', None)
        retry_after = getattr(exc, 'retry_after', None)
        if retry_after is None and headers:
            retry_after = headers.get('Retry-After')
        self.log('cache_retry_failed', stage, error_type=type(exc).__name__,
                 error=str(exc), http_status=status, retry_after=retry_after)

    def remaining(self):
        return self.deadline - self.clock()

    def wait(self, delay, stage):
        if delay >= self.remaining():
            raise self.error('budget exhausted while waiting for ' + stage)
        self.sleep(delay)

    def error(self, reason):
        details = '; '.join('{}: {}'.format(stage, self.errors[stage]) for stage in sorted(self.errors))
        return CacheVerificationError('cache verification failed for {}@{}: {}; purge_id={}, purge_status={}; {}'.format(
            self.path, self.branch, reason, self.purge_id, self.purge_status,
            details or 'no verified client bytes'))

    def request(self, url, stage, purge=False):
        for attempt in range(3):
            cooldown = self.cooldowns.get(stage, 0) - self.clock()
            if cooldown > 0:
                self.wait(cooldown, stage + ' Retry-After')
            remaining = self.remaining()
            if remaining <= 0:
                raise self.error('budget exhausted before ' + stage)
            self.attempts[stage] = self.attempts.get(stage, 0) + 1
            try:
                body = self.fetch(url, attempts=1, timeout=min(HTTP_TIMEOUT, remaining))
                if self.remaining() <= 0:
                    raise self.error('budget exhausted during ' + stage)
                return body
            except expected_errors() as exc:
                self.failure(stage, exc)
                status = getattr(exc, 'status', getattr(exc, 'code', None))
                # A lost purge response may already have created a task. Only a
                # definite rate-limit rejection is safe to submit again here.
                retryable = status == 429 if purge else status is None or status in (429, 500, 502, 503, 504)
                headers = getattr(exc, 'headers', None)
                value = getattr(exc, 'retry_after', None)
                if value is None and headers:
                    value = headers.get('Retry-After')
                delay = retry_after_seconds(value, self.wall_clock)
                if delay is not None:
                    # The header also constrains the next propagation cycle
                    # after all immediate transport retries have failed.
                    self.cooldowns[stage] = self.clock() + delay
                if not retryable or attempt == 2:
                    raise
                self.wait(BACKOFF[attempt] if delay is None else delay, stage)
            except Exception as exc:
                if not isinstance(exc, CacheVerificationError):
                    self.log('cache_unexpected_error', stage, error_type=type(exc).__name__, error=str(exc))
                raise

    def probe(self):
        try:
            body = self.request(self.cdn_url, 'cdn_request')
        except expected_errors():
            return False, False
        actual_hash = contract.digest(body)
        if actual_hash == self.expected_hash:
            if self.purge_id and not self.purge_finished:
                warning = 'purge task not finished; client bytes already verified'
                if warning not in self.warnings:
                    self.warnings.append(warning)
            self.log('cache_verified', 'cdn_request', purge_requested=self.purge_started,
                     purge_id=self.purge_id, purge_status=self.purge_status, warnings=self.warnings)
            return True, False
        self.attempts['cdn_digest'] = self.attempts.get('cdn_digest', 0) + 1
        self.failure('cdn_digest', ValueError('SHA-256 mismatch: expected={}, actual={}, bytes={}'.format(
            self.expected_hash, actual_hash, len(body))))
        return False, True

    def accept_status(self, body, stage):
        try:
            result = json.loads(body.decode('utf-8'))
            if not isinstance(result, dict) or not isinstance(result.get('status'), str) or not result['status']:
                raise ValueError('invalid purge status response')
            self.purge_status = result['status']
            if stage == 'purge_request':
                task_id = result.get('id')
                if isinstance(task_id, str) and task_id and len(task_id) <= 256:
                    self.purge_id = task_id
                elif result['status'] != 'finished':
                    raise ValueError('unfinished purge response has no valid task id')
            self.log('cache_purge_status', stage, purge_id=self.purge_id, purge_status=self.purge_status)
            if result['status'] == 'finished':
                self.purge_finished = True
                paths = result.get('paths')
                item = paths.get('/gh/' + self.key) if isinstance(paths, dict) else None
                if not isinstance(item, dict):
                    raise ValueError('finished purge response has no requested path details')
                self.log('cache_purge_result', stage, throttled=item.get('throttled'),
                         providers=item.get('providers'))
                providers = item.get('providers')
                if item.get('throttled') or not isinstance(providers, dict) or not providers or any(
                        success is not True for success in providers.values()):
                    warning = 'purge throttled or provider result not successful'
                    if warning not in self.warnings:
                        self.warnings.append(warning)
        except (UnicodeError, ValueError) as exc:
            self.failure(stage, exc)
            warning = '{}: {}'.format(stage, exc)
            if warning not in self.warnings:
                self.warnings.append(warning)

    def purge(self):
        self.purge_started = True
        try:
            body = self.request('https://purge.jsdelivr.net/gh/' + self.key, 'purge_request', purge=True)
            self.accept_status(body, 'purge_request')
        except expected_errors() as exc:
            self.warnings.append('purge_request: ' + str(exc))

    def poll(self):
        if self.purge_id and not self.purge_finished:
            try:
                url = 'https://purge.jsdelivr.net/status/' + urllib.parse.quote(self.purge_id, safe='')
                self.accept_status(self.request(url, 'purge_status'), 'purge_status')
            except expected_errors() as exc:
                warning = 'purge_status: ' + str(exc)
                if warning not in self.warnings:
                    self.warnings.append(warning)

    def run(self):
        cycle = 0
        while self.remaining() > 0:
            verified, stale = self.probe()
            if verified:
                return self.result()
            if stale and not self.purge_started:
                self.purge()
            self.poll()
            delay = BACKOFF[min(cycle, len(BACKOFF) - 1)]
            if delay >= self.remaining():
                # Use the remaining request budget for one last byte check,
                # rather than fail merely because another full wait will not fit.
                if self.remaining() > 0 and self.probe()[0]:
                    return self.result()
                raise self.error('budget exhausted while waiting for cdn propagation')
            self.wait(delay, 'cdn propagation')
            cycle += 1
        raise self.error('budget exhausted')

    def result(self):
        return {'path': self.path, 'branch': self.branch, 'cache_verified': True,
                'purge_requested': self.purge_started, 'warnings': self.warnings}


def refresh_file(path, expected_hash, branch='published', fetch=contract.fetch, sleep=time.sleep,
                 clock=time.monotonic, wall_clock=time.time, budget=FILE_BUDGET):
    if path not in {contract.TEMPLATE} | {'rule/' + name for name in contract.SYNC_RULES}:
        raise ValueError('unapproved purge path')
    if branch not in ('published', 'main') or branch == 'main' and path != contract.TEMPLATE:
        raise ValueError('unapproved purge branch')
    if not isinstance(expected_hash, str) or not contract.DIGEST.fullmatch(expected_hash):
        raise ValueError('invalid expected hash')
    if budget <= 0:
        raise ValueError('invalid cache verification budget')
    return FileRefresh(path, expected_hash, branch, fetch, sleep, clock, wall_clock, budget).run()


def refresh(publication_sha=None):
    if publication_sha:
        if not contract.SHA.fullmatch(publication_sha):
            raise ValueError('invalid published revision')
        url = 'https://raw.githubusercontent.com/{}/{}/manifest.json'.format(contract.REPOSITORY, publication_sha)
    else:
        url = 'https://raw.githubusercontent.com/{}/published/manifest.json'.format(contract.REPOSITORY)
    manifest = contract.validate_manifest(json.loads(contract.fetch(url).decode('utf-8')))
    targets = [(path, item['sha256'], 'published') for path, item in sorted(manifest['files'].items())]
    targets.append((contract.TEMPLATE, manifest['files'][contract.TEMPLATE]['sha256'], 'main'))
    failures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as workers:
        pending = {(path, branch): workers.submit(refresh_file, path, digest, branch=branch)
                   for path, digest, branch in targets}
        for (path, branch), result in pending.items():
            try:
                result.result()
            except Exception as exc:
                # Aggregate every result, but do not turn programming failures
                # into retries or a green cache status.
                print(json.dumps({'event': 'cache_verification_failed', 'path': path, 'branch': branch,
                                  'error_type': type(exc).__name__, 'error': str(exc)}, ensure_ascii=True), flush=True)
                failures.append('{}@{}: {}'.format(path, branch, exc))
    status = '缓存刷新失败，已验证规则仍已发布' if failures else '缓存刷新及文件哈希校验成功'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(status + '。\n')
            if failures:
                stream.write('未通过的缓存检查：\n')
                for failure in failures:
                    stream.write('- ' + failure.replace('\n', ' ').replace('\r', ' ') + '\n')
    print(json.dumps({'event': 'cache_refresh_result', 'cache_verified': not failures, 'source_sha': manifest['source_sha'],
                      'published': True, 'failures': failures}, ensure_ascii=True), flush=True)
    if failures:
        raise RuntimeError(status + ': ' + '; '.join(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--publication-sha')
    args = parser.parse_args()
    refresh(args.publication_sha)
