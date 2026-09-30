# Diagnostic reproductions for docs/code-review-2026-09-30.md.
# Writes synthetic fixtures under a temporary directory; does not update an install.
# Requires the Hermes runtime dependencies. See the review for invocation.

import asyncio
import contextlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'hermes'))
sys.path.insert(0, str(REPO / 'bridge'))

class SwapReached(Exception):
    pass

def release_probe(root, *, current, latest, checksum_failure=False):
    from mercury_cli import update_release as ur
    import platform
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
    name = f'mercury-{latest}-{arch}.tar.gz'
    source = root / 'fixture'
    (source / 'bin').mkdir(parents=True)
    (source / 'bin' / 'mercury').write_text('#!/bin/sh\n')
    tar = root / 'fixture.tar.gz'
    with tarfile.open(tar, 'w:gz') as tf:
        tf.add(source, arcname='mercury')
    rel = {'tag_name': f'v{latest}', 'assets': [
        {'name': name, 'browser_download_url': 'fixture:tar'},
        {'name': name + '.sha256', 'browser_download_url': 'fixture:sha'},
    ]}
    def download(url, dest):
        if url == 'fixture:tar':
            shutil.copyfile(tar, dest)
        elif checksum_failure:
            raise OSError('simulated checksum sidecar timeout')
        else:
            dest.write_text(ur._sha256(tar))
    reached = False
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        for key, value in {
            '_project_root': lambda: root / 'install',
            '_install_channel': lambda: 'nightly',
            '_latest_release': lambda **kw: rel,
            '_installed_version': lambda: current,
            '_installed_build_id': lambda: 'a' * 64,
            '_release_sha256': lambda *args: 'b' * 64,
            '_download': download,
            '_swap_tree': lambda *args: (_ for _ in ()).throw(SwapReached()),
        }.items():
            stack.enter_context(patch.object(ur, key, value))
        stack.enter_context(contextlib.redirect_stdout(output))
        try:
            ur.update_from_release(assume_yes=True)
        except SwapReached:
            reached = True
    print(f'UPDATE {current} -> {latest}; checksum_failure={checksum_failure}; swap_reached={reached}')
    for line in output.getvalue().splitlines():
        if 'same tag' in line or 'checksum step failed' in line:
            print(line)

async def inbound_probe():
    from plugins.platforms.irc.adapter import IRCAdapter
    adapter = IRCAdapter(SimpleNamespace(extra={'server': '127.0.0.1', 'nickname': 'gateway', 'channel': '#room', 'use_tls': False}))
    received = []
    async def capture(sender, target, text, **kwargs):
        received.append(text)
    adapter._route_text = capture
    for line in (
        ':owner BATCH +a draft/multiline #room',
        '@batch=a :owner!u@h PRIVMSG #room :abc',
        '@batch=a;draft/multiline-concat :owner!u@h PRIVMSG #room :def',
        ':owner BATCH -a',
    ):
        await adapter._handle_line(line)
    print(f'INGRESS expected=abcdef; received={received!r}')

async def standalone_probe():
    from plugins.platforms.irc import adapter as module
    wire = []
    done = asyncio.Event()
    async def peer(reader, writer):
        try:
            while raw := await reader.readline():
                line = raw.decode().rstrip('\r\n')
                wire.append(line)
                if line.startswith('USER '):
                    writer.write(b':test 001 bot :welcome\r\n')
                    await writer.drain()
                elif line.startswith('JOIN '):
                    writer.write(b':test 366 bot #room :joined\r\n')
                    await writer.drain()
                elif line.startswith('QUIT '):
                    break
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()
    server = await asyncio.start_server(peer, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    config = SimpleNamespace(extra={'server': '127.0.0.1', 'port': port, 'channel': '#room', 'nickname': 'bot', 'use_tls': False})
    payload = '```sh\nprintf "%s" "$HOME" **/*.py\n\n# done\n```'
    try:
        with patch.object(module, 'get_env_value', return_value=None), patch.object(module, '_get_scoped_secret', return_value=None):
            result = await module._standalone_send(config, '#room', payload)
        await asyncio.wait_for(done.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
    print(f'STANDALONE success={result.get("success")}; input={payload!r}')
    print(f'STANDALONE wire={[line for line in wire if line.startswith("PRIVMSG ")]!r}')

def denial_probe():
    import bridge
    import yaml
    configs = (
        'hermes:\n  approvals:\n    deny: ["*git push*"]\n',
        'hermes:\n  approvals:\n    deny:\n      - "*git push*"\n',
    )
    for text in configs:
        actual = yaml.safe_load(text)['hermes']['approvals']['deny']
        print(f'DENY effective_yaml={actual!r}; bridge={bridge._hermes_deny_globs(text)!r}')

def upload_probe(root):
    from observatory import lounge
    user_home = root / 'operator'
    user_home.mkdir()
    keys = root / 'keys-outside-home'
    keys.mkdir()
    (keys / 'id_ed25519').write_text('SYNTHETIC TEST KEY ONLY\n')
    (user_home / '.ssh').symlink_to(keys, target_is_directory=True)
    mercury_home = root / 'mercury'
    with patch.object(Path, 'home', return_value=user_home):
        result = lounge.stage_lounge_upload(mercury_home, user_home / '.ssh' / 'id_ed25519')
    print(f'UPLOAD symlinked .ssh key staged={result.get("filename") == "id_ed25519"}')


def render_probe(root):
    import bridge
    import yaml
    target = root / 'render-config.yaml'
    target.write_text('models:\n  default: p/chat\n  delegate_model: p/code\nhermes:\n  approvals:\n    deny: ["*git push*"]\nomp:\n  theme:\n    dark: custom\n')
    before = yaml.safe_load(target.read_text())['omp']
    slots = bridge.parse_config(str(target))
    bridge.render_omp_subtree(slots, str(target))
    after = yaml.safe_load(target.read_text())['omp']
    print(f'RENDER before={before!r}; theme_preserved={"theme" in after}; deny_preserved={"bash" in after}')

async def main():
    with tempfile.TemporaryDirectory(prefix='mercury-review-probe-') as folder:
        root = Path(folder)
        os.environ['MERCURY_HOME'] = str(root / 'mercury')
        os.environ['HERMES_HOME'] = str(root / 'hermes')
        for i, (current, latest, failed) in enumerate((('0.2.20', '0.2.19', False), ('0.2.19', '0.2.20', True))):
            case = root / str(i)
            case.mkdir()
            release_probe(case, current=current, latest=latest, checksum_failure=failed)
        denial_probe()
        render_probe(root)
        upload_probe(root)
        await inbound_probe()
        await standalone_probe()

if __name__ == '__main__':
    asyncio.run(main())
