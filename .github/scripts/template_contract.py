#!/usr/bin/env python3
"""Shared public-template contract. Compatible with the VPS Python 3.6 runtime."""
import base64
import hashlib
import ipaddress
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

REPOSITORY = 'Yaoser-Archive/Custom_OpenClash_Rules'
UPSTREAM = 'Aethersailor/Custom_OpenClash_Rules'
TEMPLATE = 'cfg/Custom_Clash_Lite.ini'
RULES = {
    'Custom_Direct_Domain.yaml': 'domain',
    'Custom_Direct_Classical_IP.yaml': 'classical',
    'Custom_Proxy_Domain.yaml': 'domain',
    'Custom_Proxy_Classical_IP.yaml': 'classical',
    'Game_Download_CDN_Domain.yaml': 'domain',
    'Game_Download_CDN_Classical_IP.yaml': 'classical',
    'Custom_Port_Direct.yaml': 'classical',
}
# Keep the previous resource available to users of older templates.
SYNC_RULES = dict(RULES)
SYNC_RULES['Steam_CDN_Classical.yaml'] = 'classical'
GROUP_DEFAULTS = {
    '手动选择': '自动选择', 'GitHub': '手动选择', '谷歌FCM': '手动选择',
    '谷歌服务': '手动选择', '苹果服务': '全球直连', '微软服务': '全球直连',
    '游戏平台': '全球直连', 'Steam': '全球直连', '测速工具': '全球直连',
    '漏网之鱼': '手动选择', '非标端口': '漏网之鱼', '全球直连': 'DIRECT',
}
GROUP_NAMES = set(GROUP_DEFAULTS) | {'自动选择', '美国节点'}
MAX_BYTES = 1024 * 1024
GOOGLE_DNS_SERVERS = ['https://8.8.8.8/dns-query', 'https://8.8.4.4/dns-query']
GOOGLE_DNS_KEYS = ['geosite:google-cn', 'geosite:googlefcm', 'geosite:google']
ORIBIT_DIRECT_HOSTS = tuple(name + '.oribit.cn' for name in
                            ('sc', 'nj', 'cyber', 'cybercd2', 'dragon', 'dsh'))
EXPECTED_RULE_COUNT = 34
CONTRACT_VERSION = 'lite-google-oribit-34-v1'
SHA = re.compile(r'^[0-9a-f]{40}$')
DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OrderedSafeDumper(yaml.SafeDumper):
    pass


OrderedSafeDumper.add_representer(dict, lambda dumper, value:
                                 dumper.represent_mapping('tag:yaml.org,2002:map', value.items()))


def dump_yaml(value):
    # PyYAML on the existing VPS predates sort_keys; do not upgrade its runtime.
    return yaml.dump(value, Dumper=OrderedSafeDumper, allow_unicode=True, width=4096)


def digest(body):
    return hashlib.sha256(body).hexdigest()


class ResourceFetchError(RuntimeError):
    """Resource failure with retry metadata, while retaining RuntimeError compatibility."""

    def __init__(self, message, status=None, retry_after=None, cause=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.cause = cause


def fetch(url, timeout=30, attempts=3, opener=urllib.request.urlopen, sleep=time.sleep):
    """Bounded HTTP GET. Never accept an HTTP error body as a resource."""
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Lite-Rule-Publisher/1'})
            with opener(request, timeout=timeout) as response:
                status = response.getcode()
                if status != 200:
                    raise urllib.error.HTTPError(url, status, 'Unexpected HTTP status',
                                                 getattr(response, 'headers', None), None)
                body = response.read(MAX_BYTES + 1)
                if not body or len(body) > MAX_BYTES:
                    raise ValueError('empty or oversized resource')
                return body
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep(2 * (attempt + 1))
    detail = 'HTTP {}'.format(last.code) if isinstance(last, urllib.error.HTTPError) else str(last)
    status = last.code if isinstance(last, urllib.error.HTTPError) else None
    headers = getattr(last, 'headers', None)
    retry_after = headers.get('Retry-After') if headers is not None else None
    raise ResourceFetchError('resource fetch failed: {}: {}'.format(type(last).__name__, detail),
                             status=status, retry_after=retry_after, cause=last) from last


def validate_rule(body, behavior):
    value = yaml.safe_load(body.decode('utf-8'))
    if not isinstance(value, dict) or set(value) != {'payload'}:
        raise ValueError('rule resource must contain only payload')
    entries = value['payload']
    if not isinstance(entries, list) or not entries:
        raise ValueError('empty or invalid payload')
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError('rule entries must be nonempty strings')
        if behavior == 'domain':
            if not re.fullmatch(r'[A-Za-z0-9_+*.-]+', entry) or not re.search(r'[A-Za-z0-9]', entry):
                raise ValueError('invalid domain rule')
            continue
        pieces = entry.split(',')
        kind = pieces[0]
        if kind in ('IP-CIDR', 'IP-CIDR6', 'SRC-IP-CIDR'):
            if len(pieces) != 3 or pieces[2] != 'no-resolve':
                raise ValueError('IP rules must keep no-resolve')
            network = ipaddress.ip_network(pieces[1], strict=False)
            if kind == 'IP-CIDR6' and network.version != 6:
                raise ValueError('invalid IPv6 rule')
        elif kind == 'GEOIP':
            if len(pieces) != 3 or pieces[2] != 'no-resolve' or not pieces[1]:
                raise ValueError('GEOIP rules must keep no-resolve')
        elif kind in ('DST-PORT', 'SRC-PORT'):
            if len(pieces) != 2:
                raise ValueError('invalid port rule')
            ports = pieces[1].split('-')
            if len(ports) not in (1, 2) or not all(p.isdigit() and 1 <= int(p) <= 65535 for p in ports):
                raise ValueError('invalid port range')
            if len(ports) == 2 and int(ports[0]) > int(ports[1]):
                raise ValueError('reversed port range')
        elif kind in ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD'):
            if len(pieces) != 2 or not pieces[1]:
                raise ValueError('invalid domain classical rule')
        else:
            raise ValueError('unsupported classical rule: ' + kind)
    return len(entries)


def rule_url(filename):
    return 'https://cdn.jsdelivr.net/gh/{}@published/rule/{}'.format(REPOSITORY, filename)


def expected_rules():
    direct, proxy = '全球直连', '手动选择'
    return [
        (direct, 'GEOSITE,private'), (direct, 'GEOIP,private,no-resolve'),
        ('谷歌服务', 'GEOSITE,google-cn'), ('谷歌FCM', 'GEOSITE,googlefcm'),
        ('谷歌服务', 'GEOSITE,google'),
        ('谷歌服务', 'IP-CIDR,8.8.8.8/32,no-resolve'),
        ('谷歌服务', 'IP-CIDR,8.8.4.4/32,no-resolve'),
        ('谷歌服务', 'GEOIP,google,no-resolve'),
    ] + [(direct, 'DOMAIN,' + host) for host in ORIBIT_DIRECT_HOSTS] + [
        (proxy, 'DOMAIN-SUFFIX,oribit.cn'),
        (direct, 'Custom_Direct_Domain.yaml'), (direct, 'Custom_Direct_Classical_IP.yaml'),
        (proxy, 'Custom_Proxy_Domain.yaml'), (proxy, 'Custom_Proxy_Classical_IP.yaml'),
        (direct, 'GEOSITE,category-games@cn'),
        (direct, 'Game_Download_CDN_Domain.yaml'), (direct, 'Game_Download_CDN_Classical_IP.yaml'),
        (direct, 'GEOSITE,category-public-tracker'),
        ('GitHub', 'GEOSITE,github'), ('测速工具', 'GEOSITE,category-speedtest'),
        ('苹果服务', 'GEOSITE,apple'), ('Steam', 'GEOSITE,steam'),
        ('微软服务', 'GEOSITE,microsoft'), (proxy, 'GEOSITE,gfw'),
        ('游戏平台', 'GEOSITE,category-games'),
        (direct, 'GEOSITE,cn'), (direct, 'GEOIP,cn,no-resolve'),
        ('非标端口', 'Custom_Port_Direct.yaml'), ('漏网之鱼', 'FINAL'),
    ]


def validate_template(body):
    groups, rules, options = {}, [], {}
    section = None
    for raw in body.decode('utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith((';', '#')):
            continue
        if line.startswith('['):
            if line != '[custom]' or section is not None:
                raise ValueError('unexpected INI section')
            section = 'custom'
            continue
        if section != 'custom' or '=' not in line:
            raise ValueError('invalid INI entry')
        key, value = line.split('=', 1)
        if key == 'custom_proxy_group':
            parts = value.split(chr(96))
            if len(parts) < 3 or parts[0] in groups or parts[1] not in ('select', 'url-test'):
                raise ValueError('invalid or duplicate group')
            groups[parts[0]] = parts
        elif key == 'ruleset':
            group, source = value.split(',', 1)
            if source.startswith('[]'):
                rules.append((group, source[2:]))
            else:
                match = re.fullmatch(r'(clash-domain|clash-classic):(https://[^,]+),([0-9]+)', source)
                if not match:
                    raise ValueError('invalid remote ruleset')
                filename = match[2].rsplit('/', 1)[-1]
                if filename not in RULES or match[2] != rule_url(filename):
                    raise ValueError('rules must use the approved published repository')
                expected_format = 'clash-domain' if RULES[filename] == 'domain' else 'clash-classic'
                interval = 1800 if filename == 'Custom_Direct_Domain.yaml' else 28800
                if match[1] != expected_format or int(match[3]) != interval:
                    raise ValueError('rule format/interval changed')
                rules.append((group, filename))
        elif key in ('enable_rule_generator', 'overwrite_original_rules'):
            if key in options:
                raise ValueError('duplicate option')
            options[key] = value
        else:
            raise ValueError('unreviewed template option: ' + key)
    if set(groups) != GROUP_NAMES or rules != expected_rules():
        raise ValueError('group contract or ordered routing contract changed')
    if options != {'enable_rule_generator': 'true', 'overwrite_original_rules': 'true'}:
        raise ValueError('rule generation must stay enabled')
    for name, default in GROUP_DEFAULTS.items():
        if groups[name][1:3] != ['select', '[]' + default]:
            raise ValueError('group default changed: ' + name)
    for name, parts in groups.items():
        for part in parts[2:]:
            if part.startswith('[]') and part[2:] not in GROUP_NAMES | {'DIRECT', 'REJECT'}:
                raise ValueError('unresolved group reference')
        if parts[1] == 'url-test':
            if len(parts) != 5 or parts[3] != 'https://cp.cloudflare.com/generate_204' or parts[4] != '300,,50':
                raise ValueError('health check behavior changed')
            re.compile(parts[2])
    references = {name: [part[2:] for part in parts[2:] if part.startswith('[]')]
                  for name, parts in groups.items()}
    validate_no_direct_routes(references)
    return groups


def validate_no_direct_routes(groups, roots=('谷歌服务', '谷歌FCM')):
    """Reject DIRECT through any selectable nested group, rather than only direct members."""
    def visit(name, ancestors):
        if name == 'DIRECT':
            raise ValueError('Google groups must never offer a direct route')
        if name not in groups:
            return
        if name in ancestors:
            raise ValueError('cyclic groups')
        for member in groups[name]:
            visit(member, ancestors | {name})
    for root in roots:
        visit(root, set())


def placeholder_links(names):
    if not names or len(set(names)) != len(names):
        raise ValueError('unique baseline names required')
    auth = base64.urlsafe_b64encode(b'aes-128-gcm:ci-placeholder').decode().rstrip('=')
    return '|'.join('ss://{}@example.invalid:1#{}'.format(auth, urllib.parse.quote(n, safe='')) for n in names)


def expected_members(names):
    proxy = ['手动选择', '自动选择', '全球直连', '美国节点']
    direct = ['全球直连', '手动选择', '自动选择', '美国节点']
    members = {name: direct + names for name in ('苹果服务', '微软服务', '游戏平台', 'Steam', '测速工具')}
    members.update({name: proxy + names for name in ('GitHub', '漏网之鱼')})
    google = ['手动选择', '自动选择', '美国节点']
    members.update({'手动选择': ['自动选择', '美国节点'] + names, '谷歌FCM': google,
                    '谷歌服务': google + names,
                    '非标端口': ['漏网之鱼', '全球直连'], '全球直连': ['DIRECT'],
                    '自动选择': names, '美国节点': names})
    return members


def convert_policy(config, names, converter='http://127.0.0.1:25500/sub'):
    query = urllib.parse.urlencode({'target': 'clash', 'url': placeholder_links(names), 'config': config})
    value = yaml.safe_load(fetch(converter + '?' + query, attempts=1).decode('utf-8'))
    validate_policy(value, names)
    return {key: value[key] for key in ('proxy-groups', 'rules', 'rule-providers')}


def validate_policy(value, names):
    if not isinstance(value, dict):
        raise ValueError('converter did not return a config')
    nodes = value.get('proxies')
    if nodes is not None and [p.get('name') for p in nodes] != names:
        raise ValueError('converter changed placeholder membership')
    group_list = value.get('proxy-groups')
    if not isinstance(group_list, list):
        raise ValueError('groups missing')
    groups = {g['name']: g for g in group_list}
    if len(groups) != len(group_list) or set(groups) != GROUP_NAMES:
        raise ValueError('converted groups changed')
    for name, default in GROUP_DEFAULTS.items():
        if groups[name].get('type') != 'select' or not groups[name].get('proxies') or groups[name]['proxies'][0] != default:
            raise ValueError('converted default changed: ' + name)
    for name in ('自动选择', '美国节点'):
        if groups[name].get('type') != 'url-test' or groups[name].get('url') != 'https://cp.cloudflare.com/generate_204':
            raise ValueError('converted health check changed')
        if groups[name].get('interval') != 300 or groups[name].get('tolerance') != 50:
            raise ValueError('converted health check timing changed')
    for group in group_list:
        members = group.get('proxies', [])
        if not members or any(n not in set(names) | GROUP_NAMES | {'DIRECT', 'REJECT'} for n in members):
            raise ValueError('empty group or unresolved member')
    for name, members in expected_members(names).items():
        if groups[name]['proxies'] != members:
            raise ValueError('group members/order changed: ' + name)
    def visit(name, ancestors):
        if name in ancestors:
            raise ValueError('cyclic groups')
        for member in groups[name]['proxies']:
            if member in groups:
                visit(member, ancestors | {name})
    for name in groups:
        visit(name, set())
    validate_no_direct_routes({name: group['proxies'] for name, group in groups.items()})
    providers = value.get('rule-providers')
    if not isinstance(providers, dict) or len(providers) != len(RULES):
        raise ValueError('rule providers changed')
    by_file = {}
    for name, provider in providers.items():
        filename = provider.get('url', '').rsplit('/', 1)[-1]
        if filename not in RULES or filename in by_file or provider['url'] != rule_url(filename):
            raise ValueError('unapproved rule provider')
        if provider.get('type') != 'http' or provider.get('behavior') != RULES[filename] or provider.get('format', 'yaml') != 'yaml':
            raise ValueError('provider format changed')
        interval = 1800 if filename == 'Custom_Direct_Domain.yaml' else 28800
        if provider.get('interval') != interval:
            raise ValueError('provider interval changed')
        path = provider.get('path', '')
        if not path or pathlib.PurePosixPath(path).is_absolute() or '..' in pathlib.PurePosixPath(path).parts:
            raise ValueError('unsafe provider cache path')
        by_file[filename] = name
    expected = []
    for group, rule in expected_rules():
        if rule in RULES:
            expected.append('RULE-SET,{},{group}'.format(by_file[rule], group=group))
        else:
            parts = rule.split(',')
            expected.append(','.join([('MATCH' if parts[0] == 'FINAL' else parts[0])] + parts[1:2] + [group] + parts[2:]))
    if value.get('rules') != expected:
        raise ValueError('converted routing order or no-resolve changed')


def make_manifest(root, source_sha, upstream_sha):
    files = {TEMPLATE: {'sha256': digest((root / TEMPLATE).read_bytes()), 'behavior': 'ini'}}
    for filename, behavior in SYNC_RULES.items():
        path = 'rule/' + filename
        files[path] = {'sha256': digest((root / path).read_bytes()), 'behavior': behavior}
    result = {'schema_version': 1, 'repository': REPOSITORY, 'source_sha': source_sha,
              'upstream_sha': upstream_sha, 'template': TEMPLATE, 'files': files}
    validate_manifest(result)
    return result


def validate_manifest(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('repository') != REPOSITORY:
        raise ValueError('invalid publication manifest')
    if not SHA.fullmatch(value.get('source_sha', '')) or not SHA.fullmatch(value.get('upstream_sha', '')):
        raise ValueError('manifest must pin source revisions')
    expected = {TEMPLATE: 'ini'}
    expected.update({'rule/' + n: b for n, b in SYNC_RULES.items()})
    files = value.get('files')
    if value.get('template') != TEMPLATE or not isinstance(files, dict) or set(files) != set(expected):
        raise ValueError('manifest file allowlist changed')
    for path, behavior in expected.items():
        item = files[path]
        if not isinstance(item, dict) or item.get('behavior') != behavior or not DIGEST.fullmatch(item.get('sha256', '')):
            raise ValueError('invalid file hash/behavior')
    return value
