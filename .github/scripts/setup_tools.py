#!/usr/bin/env python3
import gzip
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import time
import urllib.request


def setup(root, destination):
    pins = json.loads((root / '.github/pinned-tools.json').read_text())
    request = urllib.request.Request(pins['mihomo_url'], headers={'User-Agent': 'Lite-Rule-Publisher/1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read(64 * 1024 * 1024 + 1)
    if len(archive) > 64 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != pins['mihomo_sha256']:
        raise RuntimeError('Mihomo release checksum mismatch')
    destination.mkdir(parents=True, exist_ok=True)
    binary = destination / 'mihomo'
    binary.write_bytes(gzip.decompress(archive))
    binary.chmod(0o700)
    request = urllib.request.Request(pins['actionlint_url'], headers={'User-Agent': 'Lite-Rule-Publisher/1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read(16 * 1024 * 1024 + 1)
    if len(archive) > 16 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != pins['actionlint_sha256']:
        raise RuntimeError('actionlint release checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        tool = destination / 'actionlint'
        tool.write_bytes(package.extractfile('actionlint').read())
        tool.chmod(0o700)
    # Read-only mount stays at a stable path while each candidate is replaced.
    subprocess.run(['docker', 'run', '--detach', '--name', 'lite-validation',
                    '--memory', '192m', '--cpus', '1',
                    '--publish', '127.0.0.1:25500:25500',
                    '--volume', str(destination / 'candidate') + ':/base/config/lite-ci:ro',
                    pins['converter_image']], check=True)
    for attempt in range(40):
        try:
            with urllib.request.urlopen('http://127.0.0.1:25500/version', timeout=1) as response:
                if response.getcode() == 200:
                    break
        except OSError:
            time.sleep(0.25)
    else:
        raise RuntimeError('isolated converter did not start')
    if os.environ.get('GITHUB_PATH'):
        with open(os.environ['GITHUB_PATH'], 'a') as stream:
            stream.write(str(destination) + '\n')


if __name__ == '__main__':
    root, destination = map(pathlib.Path, sys.argv[1:3])
    (destination / 'candidate').mkdir(parents=True, exist_ok=True)
    setup(root.resolve(), destination.resolve())
