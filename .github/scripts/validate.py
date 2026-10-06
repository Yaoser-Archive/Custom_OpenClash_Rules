#!/usr/bin/env python3
"""Validate a candidate snapshot with the same conversion engine as production."""
import argparse
import copy
import json
import pathlib
import shutil
import subprocess
import tempfile

import yaml
import template_contract as contract

FIXTURE_NAMES = ['US-CF-' + str(i) for i in range(10)]


def validate(root, config_path, core, converter='http://127.0.0.1:25500/sub'):
    contract.validate_template((root / contract.TEMPLATE).read_bytes())
    counts = {}
    for filename, behavior in contract.SYNC_RULES.items():
        counts[filename] = contract.validate_rule((root / 'rule' / filename).read_bytes(), behavior)
    policy = contract.convert_policy(config_path, FIXTURE_NAMES, converter)
    auth_nodes = [{'name': n, 'type': 'ss', 'server': 'example.invalid', 'port': 1,
                   'cipher': 'aes-128-gcm', 'password': 'ci-placeholder'} for n in FIXTURE_NAMES]
    with tempfile.TemporaryDirectory(prefix='lite-core-') as tmp:
        work = pathlib.Path(tmp)
        value = copy.deepcopy(policy)
        value.update({'proxies': auth_nodes, 'mode': 'rule', 'log-level': 'silent',
                      'allow-lan': False, 'mixed-port': 0, 'external-controller': ''})
        for provider in value['rule-providers'].values():
            filename = provider['url'].rsplit('/', 1)[-1]
            target = work / 'rules' / filename
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(str(root / 'rule' / filename), str(target))
            provider.update({'type': 'file', 'path': 'rules/' + filename})
            provider.pop('url', None)
            provider.pop('interval', None)
        config = work / 'config.yaml'
        config.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
        checked = subprocess.run([str(core), '-t', '-d', str(work), '-f', str(config)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        if checked.returncode:
            raise RuntimeError('Mihomo validation failed: ' + checked.stdout.decode(errors='replace')[-4000:])
    print(json.dumps({'validated': True, 'nodes': len(FIXTURE_NAMES), 'groups': len(policy['proxy-groups']),
                      'rules': len(policy['rules']), 'payload_counts': counts}, ensure_ascii=False))
    return policy


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=pathlib.Path, required=True)
    parser.add_argument('--config-path', required=True)
    parser.add_argument('--core', type=pathlib.Path, required=True)
    parser.add_argument('--converter', default='http://127.0.0.1:25500/sub')
    args = parser.parse_args()
    validate(args.root, args.config_path, args.core, args.converter)
