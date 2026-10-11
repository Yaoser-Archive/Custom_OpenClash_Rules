import copy
import io
import pathlib
import unittest
import urllib.error

import template_contract as contract


ROOT = pathlib.Path(__file__).resolve().parents[2]


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status


def converted_fixture():
    names = ['US-CF-0', 'US-CF-1']
    members = contract.expected_members(names)
    groups = []
    for name, choices in members.items():
        group = {'name': name, 'type': 'select', 'proxies': choices}
        if name in ('自动选择', '美国节点'):
            group.update(type='url-test', url='https://cp.cloudflare.com/generate_204',
                         interval=300, tolerance=50)
        groups.append(group)
    providers = {}
    identifiers = {}
    for index, (filename, behavior) in enumerate(contract.RULES.items()):
        identifier = 'provider-' + str(index)
        identifiers[filename] = identifier
        providers[identifier] = {
            'type': 'http', 'behavior': behavior, 'format': 'yaml',
            'url': contract.rule_url(filename), 'path': './rules/' + filename,
            'interval': 1800 if filename == 'Custom_Direct_Domain.yaml' else 28800,
        }
    rules = []
    for group, source in contract.expected_rules():
        if source in identifiers:
            rules.append('RULE-SET,' + identifiers[source] + ',' + group)
        else:
            parts = source.split(',')
            rules.append(','.join([('MATCH' if parts[0] == 'FINAL' else parts[0])] +
                                  parts[1:2] + [group] + parts[2:]))
    return {'proxy-groups': groups, 'rule-providers': providers, 'rules': rules}, names


class RoutingContractTests(unittest.TestCase):
    def test_template_has_google_then_exact_oribit_before_public_direct_rules(self):
        text = (ROOT / contract.TEMPLATE).read_text(encoding='utf-8')
        contract.validate_template(text.encode('utf-8'))
        prefix = [line for line in text.splitlines() if line.startswith('ruleset=')]
        expected_prefix = [
            'ruleset=全球直连,[]GEOSITE,private',
            'ruleset=全球直连,[]GEOIP,private,no-resolve',
            'ruleset=谷歌服务,[]GEOSITE,google-cn',
            'ruleset=谷歌FCM,[]GEOSITE,googlefcm',
            'ruleset=谷歌服务,[]GEOSITE,google',
            'ruleset=谷歌服务,[]IP-CIDR,8.8.8.8/32,no-resolve',
            'ruleset=谷歌服务,[]IP-CIDR,8.8.4.4/32,no-resolve',
            'ruleset=谷歌服务,[]GEOIP,google,no-resolve',
        ] + ['ruleset=全球直连,[]DOMAIN,' + name + '.oribit.cn'
             for name in ('sc', 'nj', 'cyber', 'cybercd2', 'dragon', 'dsh')] + [
            'ruleset=手动选择,[]DOMAIN-SUFFIX,oribit.cn',
        ]
        self.assertEqual(prefix[:15], expected_prefix)
        self.assertEqual(len(prefix), 34)
        self.assertEqual(len(contract.expected_rules()), contract.EXPECTED_RULE_COUNT)
        self.assertIn('Custom_Direct_Domain.yaml', prefix[15])

    def test_template_rejects_suffix_direct_or_parent_preceding_exceptions(self):
        text = (ROOT / contract.TEMPLATE).read_text(encoding='utf-8')
        exact = 'ruleset=全球直连,[]DOMAIN,sc.oribit.cn'
        parent = 'ruleset=手动选择,[]DOMAIN-SUFFIX,oribit.cn'
        changes = [text.replace(exact, exact.replace('[]DOMAIN,', '[]DOMAIN-SUFFIX,')),
                   text.replace(exact, parent).replace(parent + '\n;本项目', exact + '\n;本项目'),
                   text.replace(parent, parent + '\n' + exact)]
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                contract.validate_template(changed.encode('utf-8'))

    def test_google_rejects_direct_via_manual_selection(self):
        text = (ROOT / contract.TEMPLATE).read_text(encoding='utf-8')
        original = 'custom_proxy_group=手动选择`select`[]自动选择`[]美国节点`.*'
        changed = text.replace(original, original + '`[]全球直连')
        with self.assertRaisesRegex(ValueError, 'direct route'):
            contract.validate_template(changed.encode('utf-8'))
        groups = {'谷歌服务': ['hand'], '谷歌FCM': ['other'],
                  'hand': ['nested'], 'nested': ['DIRECT'], 'other': ['node']}
        with self.assertRaisesRegex(ValueError, 'direct route'):
            contract.validate_no_direct_routes(groups)
        groups['nested'] = ['node']
        contract.validate_no_direct_routes(groups)
        groups['nested'] = ['hand']
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            contract.validate_no_direct_routes(groups)

    def test_converter_contract_rejects_broadened_exceptions_and_mixed_order(self):
        policy, names = converted_fixture()
        contract.validate_policy(policy, names)
        self.assertEqual(policy['rules'][8], 'DOMAIN,sc.oribit.cn,全球直连')
        self.assertEqual(policy['rules'][14], 'DOMAIN-SUFFIX,oribit.cn,手动选择')
        self.assertEqual(len(policy['rules']), 34)
        for mutation in ('suffix', 'order', 'google-direct', 'google-dns-resolve'):
            bad = copy.deepcopy(policy)
            if mutation == 'suffix':
                bad['rules'][8] = 'DOMAIN-SUFFIX,sc.oribit.cn,全球直连'
            elif mutation == 'order':
                bad['rules'][8], bad['rules'][14] = bad['rules'][14], bad['rules'][8]
            elif mutation == 'google-direct':
                next(g for g in bad['proxy-groups'] if g['name'] == '手动选择')['proxies'].append('全球直连')
            else:
                bad['rules'][5] = 'IP-CIDR,8.8.8.8/32,谷歌服务'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                contract.validate_policy(bad, names)


class FetchMetadataTests(unittest.TestCase):
    def test_http_failure_retains_status_retry_after_and_cause(self):
        for retry_after in ('17', 'Wed, 21 Oct 2026 07:28:00 GMT'):
            error = urllib.error.HTTPError('https://example.invalid', 429, 'rate limited',
                                           {'Retry-After': retry_after}, None)
            def fail(*args, **kwargs):
                raise error
            with self.assertRaises(contract.ResourceFetchError) as caught:
                contract.fetch('https://example.invalid', attempts=1, opener=fail)
            self.assertIsInstance(caught.exception, RuntimeError)
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(caught.exception.retry_after, retry_after)
            self.assertIs(caught.exception.cause, error)
            self.assertIn('HTTP 429', str(caught.exception))

    def test_non_200_returned_response_is_rejected_with_metadata(self):
        response = Response(b'untrusted-error-body', status=503, headers={'Retry-After': '9'})
        with self.assertRaises(contract.ResourceFetchError) as caught:
            contract.fetch('https://example.invalid', attempts=1,
                           opener=lambda *args, **kwargs: response)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.retry_after, '9')

    def test_timeout_and_payload_error_keep_underlying_reason(self):
        timeout = TimeoutError('socket timed out')
        def fail(*args, **kwargs):
            raise timeout
        with self.assertRaises(contract.ResourceFetchError) as caught:
            contract.fetch('https://example.invalid', attempts=1, opener=fail)
        self.assertIs(caught.exception.cause, timeout)
        self.assertIsNone(caught.exception.status)
        self.assertIsNone(caught.exception.retry_after)
        self.assertIn('socket timed out', str(caught.exception))
        with self.assertRaisesRegex(contract.ResourceFetchError, 'empty or oversized'):
            contract.fetch('https://example.invalid', attempts=1,
                           opener=lambda *args, **kwargs: Response(b''))

    def test_fetch_still_returns_bytes_after_transient_failure(self):
        calls, pauses = [], []
        def opener(*args, **kwargs):
            calls.append(kwargs['timeout'])
            if len(calls) == 1:
                raise urllib.error.HTTPError('https://example.invalid', 503, 'Unavailable', {}, None)
            return Response(b'approved')
        self.assertEqual(contract.fetch('https://example.invalid', timeout=7, opener=opener,
                                       sleep=pauses.append), b'approved')
        self.assertEqual(calls, [7, 7])
        self.assertEqual(pauses, [2])


if __name__ == '__main__':
    unittest.main()
