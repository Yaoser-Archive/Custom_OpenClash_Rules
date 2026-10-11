import email.utils
import io
import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import cache_refresh
import template_contract as contract


BODY = b'approved client resource'
DIGEST = contract.digest(BODY)
PATH = contract.TEMPLATE
KEY = '/gh/' + contract.REPOSITORY + '@published/' + PATH


class FakeClock:
    def __init__(self):
        self.value = 0
        self.waits = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


def finished(throttled=False, providers=None):
    return json.dumps({'id': 'task-1', 'status': 'finished', 'paths': {
        KEY: {'throttled': throttled, 'providers': {'CF': True, 'FY': True} if providers is None else providers}
    }}).encode()


class CacheRefreshTests(unittest.TestCase):
    def run_file(self, fetch, timer=None, **kwargs):
        timer = timer or FakeClock()
        output = io.StringIO()
        with mock.patch('sys.stdout', output):
            result = cache_refresh.refresh_file(PATH, DIGEST, fetch=fetch, sleep=timer.sleep,
                                                clock=timer.now, wall_clock=lambda: 1000, **kwargs)
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_correct_client_bytes_skip_purge(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            return BODY
        result, events = self.run_file(fetch)
        self.assertTrue(result['cache_verified'])
        self.assertFalse(result['purge_requested'])
        self.assertEqual(len(calls), 1)
        self.assertIn('cdn.jsdelivr.net', calls[0][0])
        self.assertNotIn('?', calls[0][0])
        self.assertEqual(calls[0][1], {'attempts': 1, 'timeout': 10})
        self.assertEqual(events[-1]['event'], 'cache_verified')
        for evidence in (result, events[-1]):
            self.assertEqual(evidence['expected_hash'], DIGEST)
            self.assertEqual(evidence['actual_hash'], DIGEST)
            self.assertEqual(evidence['stage'], 'cdn_request')
            self.assertEqual(evidence['attempts'], {'cdn_request': 1})
            self.assertEqual(evidence['http_errors'], [])
            self.assertIsNone(evidence['purge_id'])
            self.assertIsNone(evidence['purge_status'])

    def test_pending_task_is_polled_without_another_purge(self):
        counts = {'cdn': 0, 'purge': 0, 'status': 0}
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            if '/status/' in url:
                counts['status'] += 1
                return finished() if counts['status'] >= 2 else b'{"status":"pending"}'
            if 'purge.jsdelivr.net' in url:
                counts['purge'] += 1
                return b'{"id":"task-1","status":"pending"}'
            counts['cdn'] += 1
            return BODY if counts['status'] >= 2 else b'stale'
        result, _ = self.run_file(fetch)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(counts['purge'], 1)
        self.assertEqual(counts['status'], 2)
        self.assertTrue(all(url.endswith('/status/task-1') for url in calls if '/status/' in url))
        self.assertTrue(all('?' not in url for url in calls))

    def test_finished_does_not_allow_stale_bytes(self):
        timer = FakeClock()
        def fetch(url, **kwargs):
            return finished() if 'purge.jsdelivr.net' in url else b'stale'
        with mock.patch('sys.stdout', io.StringIO()), self.assertRaisesRegex(
                cache_refresh.CacheVerificationError, contract.digest(b'stale')) as error:
            self.run_file(fetch, timer, budget=15)
        self.assertLessEqual(timer.value, 15)
        evidence = error.exception.cache_result
        self.assertFalse(evidence['cache_verified'])
        self.assertEqual(evidence['actual_hash'], contract.digest(b'stale'))
        self.assertEqual(evidence['expected_hash'], DIGEST)
        self.assertEqual(evidence['purge_status'], 'finished')
        self.assertTrue(evidence['purge_finished'])
        self.assertGreater(evidence['attempts']['cdn_digest'], 0)

    def test_pending_forever_is_bounded_and_purge_is_not_repeated(self):
        timer = FakeClock()
        counts = {'purge': 0, 'status': 0}
        def fetch(url, **kwargs):
            if '/status/' in url:
                counts['status'] += 1
                return b'{"status":"pending"}'
            if 'purge.jsdelivr.net' in url:
                counts['purge'] += 1
                return b'{"id":"task-1","status":"pending"}'
            return b'stale'
        with mock.patch('sys.stdout', io.StringIO()), self.assertRaisesRegex(
                cache_refresh.CacheVerificationError, 'purge_id=task-1, purge_status=pending'):
            self.run_file(fetch, timer)
        self.assertEqual(counts['purge'], 1)
        self.assertGreater(counts['status'], 1)
        self.assertLessEqual(timer.value, 120)

    def test_final_byte_check_can_succeed_when_full_wait_will_not_fit(self):
        timer = FakeClock()
        counts = {'cdn': 0}
        def fetch(url, **kwargs):
            if 'purge.jsdelivr.net' in url:
                return finished()
            counts['cdn'] += 1
            return BODY if counts['cdn'] >= 4 else b'stale'
        result, _ = self.run_file(fetch, timer, budget=10)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(counts['cdn'], 4)
        self.assertLessEqual(timer.value, 10)

    def test_task_id_is_encoded_as_one_status_path_segment(self):
        calls, counts = [], {'cdn': 0}
        def fetch(url, **kwargs):
            calls.append(url)
            if '/status/' in url:
                return finished()
            if 'purge.jsdelivr.net' in url:
                return b'{"id":"task/?#","status":"pending"}'
            counts['cdn'] += 1
            return BODY if counts['cdn'] > 1 else b'stale'
        self.run_file(fetch)
        self.assertTrue(any(url.endswith('/status/task%2F%3F%23') for url in calls))

    def test_initial_network_errors_are_retried_without_purge(self):
        calls = []
        timer = FakeClock()
        def fetch(url, **kwargs):
            calls.append(url)
            if len(calls) < 3:
                raise TimeoutError('read timed out')
            return BODY
        result, events = self.run_file(fetch, timer)
        self.assertTrue(result['cache_verified'])
        self.assertFalse(result['purge_requested'])
        self.assertEqual(timer.waits, [2, 5])
        self.assertTrue(all('cdn.jsdelivr.net' in url for url in calls))
        self.assertEqual([event['stage'] for event in events[:-1]], ['cdn_request', 'cdn_request'])

    def test_persistent_read_failure_does_not_trigger_purge(self):
        calls = []
        timer = FakeClock()
        def fetch(url, **kwargs):
            calls.append(url)
            raise TimeoutError('read timed out')
        with mock.patch('sys.stdout', io.StringIO()), self.assertRaises(cache_refresh.CacheVerificationError) as error:
            self.run_file(fetch, timer, budget=30)
        self.assertTrue(all('cdn.jsdelivr.net' in url for url in calls))
        self.assertLessEqual(timer.value, 30)
        evidence = error.exception.cache_result
        self.assertIsNone(evidence['actual_hash'])
        self.assertEqual(evidence['expected_hash'], DIGEST)
        self.assertEqual(evidence['stage'], 'cdn_request')
        self.assertEqual(len(evidence['http_errors']), len(calls))
        self.assertIsNone(evidence['http_errors'][0]['http_status'])
        self.assertEqual(evidence['http_errors'][0]['error_type'], 'TimeoutError')

    def test_lost_purge_response_is_not_resubmitted(self):
        counts = {'cdn': 0, 'purge': 0}
        def fetch(url, **kwargs):
            if 'purge.jsdelivr.net' in url:
                counts['purge'] += 1
                raise TimeoutError('purge response lost')
            counts['cdn'] += 1
            return BODY if counts['cdn'] > 1 else b'stale'
        result, _ = self.run_file(fetch)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(counts['purge'], 1)
        self.assertIn('purge response lost', result['warnings'][0])

    def test_purge_503_is_not_blindly_resubmitted(self):
        counts = {'cdn': 0, 'purge': 0}
        def fetch(url, **kwargs):
            if 'purge.jsdelivr.net' in url:
                counts['purge'] += 1
                raise contract.ResourceFetchError('HTTP 503', status=503, retry_after='2')
            counts['cdn'] += 1
            return BODY if counts['cdn'] > 1 else b'stale'
        result, _ = self.run_file(fetch)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(counts['purge'], 1)

    def test_definite_purge_429_respects_retry_after(self):
        counts = {'cdn': 0, 'purge': 0}
        timer = FakeClock()
        def fetch(url, **kwargs):
            if 'purge.jsdelivr.net' in url:
                counts['purge'] += 1
                if counts['purge'] == 1:
                    raise contract.ResourceFetchError('HTTP 429', status=429, retry_after='8')
                return finished()
            counts['cdn'] += 1
            return BODY if counts['cdn'] > 1 else b'stale'
        result, events = self.run_file(fetch, timer)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(counts['purge'], 2)
        self.assertEqual(timer.waits[0], 8)
        rate_limit = [event for event in events if event.get('http_status') == 429][0]
        self.assertEqual(rate_limit['retry_after'], '8')
        self.assertEqual(result['http_errors'], [dict(stage='purge_request', attempt=1,
                                                    error_type='ResourceFetchError', error='HTTP 429',
                                                    http_status=429, retry_after='8')])
        self.assertEqual(result['purge_status'], 'finished')
        self.assertEqual(result['actual_hash'], DIGEST)

    def test_retry_after_date_invalid_header_and_budget(self):
        date = email.utils.formatdate(1015, usegmt=True)
        self.assertEqual(cache_refresh.retry_after_seconds(date, lambda: 1000), 15)
        self.assertEqual(cache_refresh.retry_after_seconds('4'), 4)
        for value in (None, '', 'bad-date', '-3'):
            self.assertIsNone(cache_refresh.retry_after_seconds(value))
        timer = FakeClock()
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            raise contract.ResourceFetchError('HTTP 429', status=429, retry_after='121')
        with mock.patch('sys.stdout', io.StringIO()), self.assertRaisesRegex(
                cache_refresh.CacheVerificationError, 'budget exhausted'):
            self.run_file(fetch, timer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(timer.waits, [])

    def test_timeout_is_clamped_to_remaining_budget(self):
        timer = FakeClock()
        timeouts = []
        def fetch(url, **kwargs):
            timeouts.append(kwargs['timeout'])
            if len(timeouts) == 1:
                timer.value += 115
                raise TimeoutError('read timed out')
            return BODY
        result, _ = self.run_file(fetch, timer)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(timeouts, [10, 3])

    def test_retry_after_survives_exhausted_transport_retries(self):
        timer = FakeClock()
        starts = []
        def fetch(url, **kwargs):
            starts.append(timer.now())
            if len(starts) <= 3:
                raise contract.ResourceFetchError('HTTP 429', status=429, retry_after='10')
            return BODY
        result, _ = self.run_file(fetch, timer)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(starts, [0, 10, 20, 30])

    def test_response_after_budget_is_not_accepted(self):
        timer = FakeClock()
        def fetch(url, **kwargs):
            timer.value += 121
            return BODY
        with self.assertRaisesRegex(cache_refresh.CacheVerificationError, 'budget exhausted during cdn_request'):
            self.run_file(fetch, timer)

    def test_status_errors_are_visible_and_final_bytes_can_succeed(self):
        counts = {'cdn': 0, 'purge': 0, 'status': 0}
        def fetch(url, **kwargs):
            if '/status/' in url:
                counts['status'] += 1
                raise TimeoutError('status unavailable')
            if 'purge.jsdelivr.net' in url:
                counts['purge'] += 1
                return b'{"id":"task-1","status":"pending"}'
            counts['cdn'] += 1
            return BODY if counts['cdn'] > 1 else b'stale'
        result, events = self.run_file(fetch)
        self.assertTrue(result['cache_verified'])
        self.assertEqual(counts['purge'], 1)
        self.assertEqual(counts['status'], 3)
        self.assertTrue(any('status unavailable' in warning for warning in result['warnings']))
        self.assertTrue(any(event['stage'] == 'purge_status' and event['event'] == 'cache_retry_failed' for event in events))

    def test_invalid_purge_responses_are_not_resubmitted(self):
        for response in (b'not-json', b'\xff', b'[]', b'{"status":"pending"}'):
            with self.subTest(response=response):
                counts = {'cdn': 0, 'purge': 0}
                def fetch(url, **kwargs):
                    if 'purge.jsdelivr.net' in url:
                        counts['purge'] += 1
                        return response
                    counts['cdn'] += 1
                    return BODY if counts['cdn'] > 1 else b'stale'
                result, events = self.run_file(fetch)
                self.assertTrue(result['cache_verified'])
                self.assertEqual(counts['purge'], 1)
                self.assertTrue(result['warnings'])
                self.assertTrue(any(event['event'] == 'cache_retry_failed' and event['stage'] == 'purge_request' for event in events))

    def test_throttle_and_provider_failure_are_retained(self):
        counts = {'cdn': 0}
        def fetch(url, **kwargs):
            if 'purge.jsdelivr.net' in url:
                return finished(True, {'CF': True, 'FY': False})
            counts['cdn'] += 1
            return BODY if counts['cdn'] > 1 else b'stale'
        result, events = self.run_file(fetch)
        self.assertTrue(result['cache_verified'])
        self.assertIn('provider result not successful', result['warnings'][0])
        purge_result = [event for event in events if event['event'] == 'cache_purge_result'][0]
        self.assertTrue(purge_result['throttled'])
        self.assertFalse(purge_result['providers']['FY'])

    def test_unknown_programming_failure_is_not_retried(self):
        for error in (RuntimeError('implementation bug'), TypeError('wrong shape')):
            with self.subTest(error=error):
                fetch = mock.Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    self.run_file(fetch)
                self.assertEqual(fetch.call_count, 1)

    def test_unapproved_target_is_rejected_before_http(self):
        fetch = mock.Mock()
        for path, branch, digest in (('../secrets', 'published', DIGEST),
                                     (PATH, 'other-branch', DIGEST),
                                     ('rule/' + next(iter(contract.SYNC_RULES)), 'main', DIGEST),
                                     (PATH, 'published', 'bad-hash')):
            with self.subTest(path=path, branch=branch, digest=digest), self.assertRaises(ValueError):
                cache_refresh.refresh_file(path, digest, branch, fetch=fetch)
        self.assertEqual(fetch.call_count, 0)

    def test_all_ten_urls_share_three_workers_and_include_main_compatibility(self):
        manifest = self.manifest()
        lock, barrier = threading.Lock(), threading.Barrier(3)
        state = {'active': 0, 'peak': 0, 'started': 0}
        calls = []
        def refresh_one(path, digest, branch):
            with lock:
                state['active'] += 1
                state['started'] += 1
                position = state['started']
                state['peak'] = max(state['peak'], state['active'])
                calls.append((path, branch))
            if position <= 3:
                barrier.wait(timeout=5)
            with lock:
                state['active'] -= 1
            return self.verified_result(path, digest, branch)
        with tempfile.TemporaryDirectory() as tmp:
            summary = pathlib.Path(tmp) / 'summary.md'
            with mock.patch.object(contract, 'fetch', return_value=json.dumps(manifest).encode()), \
                    mock.patch.object(cache_refresh, 'refresh_file', side_effect=refresh_one), \
                    mock.patch('sys.stdout', io.StringIO()), \
                    mock.patch.dict(cache_refresh.os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}, clear=True):
                cache_refresh.refresh('a' * 40)
            records = json.loads(summary.read_text(encoding='utf-8').split('```json\n')[1].split('\n```')[0])
        self.assertEqual(len(calls), 10)
        self.assertEqual(state['peak'], 3)
        self.assertIn((PATH, 'main'), calls)
        self.assertEqual(len([branch for _, branch in calls if branch == 'published']), 9)
        self.assertEqual(len(records), 10)
        self.assertEqual({(item['path'], item['branch']) for item in records}, set(calls))
        for item in records:
            self.assertTrue(item['cache_verified'])
            self.assertEqual(item['expected_hash'], DIGEST)
            self.assertEqual(item['actual_hash'], DIGEST)
            self.assertEqual(item['attempts'], {'cdn_request': 1})
            self.assertEqual(item['stage'], 'cdn_request')
            self.assertEqual(item['http_errors'], [])
            self.assertIsNone(item['purge_status'])

    def test_failures_keep_publication_state_and_collect_every_result(self):
        manifest = self.manifest()
        calls, output = [], io.StringIO()
        real_refresh_file = cache_refresh.refresh_file
        def refresh_one(path, digest, branch):
            calls.append((path, branch))
            if path == PATH and branch == 'main':
                raise TypeError('unexpected bug')
            if path == PATH:
                timer = FakeClock()
                return real_refresh_file(path, digest, branch=branch,
                                         fetch=lambda url, **kw: finished() if 'purge.jsdelivr.net' in url else b'stale',
                                         clock=timer.now, sleep=timer.sleep, budget=3)
            return self.verified_result(path, digest, branch)
        with tempfile.TemporaryDirectory() as tmp:
            summary = pathlib.Path(tmp) / 'summary.md'
            with mock.patch.object(contract, 'fetch', return_value=json.dumps(manifest).encode()), \
                    mock.patch.object(cache_refresh, 'refresh_file', side_effect=refresh_one), \
                    mock.patch('sys.stdout', output), mock.patch.dict(cache_refresh.os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}, clear=True), \
                    self.assertRaisesRegex(RuntimeError, '已验证规则仍已发布'):
                cache_refresh.refresh('a' * 40)
            text = summary.read_text(encoding='utf-8')
        self.assertEqual(len(calls), 10)
        self.assertIn(PATH + '@main', text)
        self.assertIn('unexpected bug', text)
        records = json.loads(text.split('```json\n')[1].split('\n```')[0])
        self.assertEqual(len(records), 10)
        failed = [item for item in records if not item['cache_verified']]
        self.assertEqual(len(failed), 2)
        for item in failed:
            self.assertEqual(item['expected_hash'], DIGEST)
            self.assertEqual(item['http_errors'], [])
            if item['branch'] == 'main':
                self.assertIsNone(item['actual_hash'])
                self.assertEqual(item['stage'], 'refresh_file')
                self.assertEqual(item['error_type'], 'TypeError')
            else:
                self.assertEqual(item['actual_hash'], contract.digest(b'stale'))
                self.assertEqual(item['stage'], 'cdn_digest')
                self.assertEqual(item['error_type'], 'CacheVerificationError')
                self.assertEqual(item['purge_status'], 'finished')
                self.assertGreater(item['attempts']['cdn_digest'], 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        failed_events = [event for event in events if event['event'] == 'cache_verification_failed']
        self.assertEqual(len(failed_events), 2)
        self.assertEqual([{key: value for key, value in event.items() if key != 'event'}
                          for event in failed_events], failed)
        self.assertFalse(events[-1]['cache_verified'])
        self.assertTrue(events[-1]['published'])

    @staticmethod
    def verified_result(path, digest, branch):
        item = cache_refresh.FileRefresh(path, digest, branch, None, None, lambda: 0, lambda: 0, 120)
        item.stage, item.actual_hash = 'cdn_request', digest
        item.attempts['cdn_request'] = 1
        return item.result()

    @staticmethod
    def manifest():
        files = {PATH: {'sha256': DIGEST, 'behavior': 'ini'}}
        files.update({'rule/' + name: {'sha256': DIGEST, 'behavior': behavior}
                      for name, behavior in contract.SYNC_RULES.items()})
        return {'schema_version': 1, 'repository': contract.REPOSITORY, 'source_sha': 'a' * 40,
                'upstream_sha': 'b' * 40, 'template': PATH, 'files': files}


if __name__ == '__main__':
    unittest.main()
