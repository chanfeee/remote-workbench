#!/usr/bin/env python3
"""Install a fixed local Git ref, smoke-test it, then switch an existing stable install.

Requires Python 3.11+ and uv. Does not fetch, move branches, edit Codex config,
restart existing clients, or run remote commands.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid


SMOKE = r'''
import asyncio, json, sys, tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import remote_workbench
from remote_workbench.inline_app import BATCH_RESOURCE_URI, RESOURCE_URI

release, config, version = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
assert remote_workbench.__version__ == version
assert Path(remote_workbench.__file__).resolve().is_relative_to(release)

async def check():
    with tempfile.TemporaryDirectory(prefix='rwb-install-smoke-') as state:
        params = StdioServerParameters(command=str(release/'venv/bin/remote-workbench-session'),
            args=['--config', config, '--data-dir', state, '--viewer-port', '0'])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) == 11
                by_name = {tool.name: tool for tool in tools.tools}
                assert 'Use exec_batch' in by_name['exec_command'].description
                assert by_name['review_activity'].annotations.readOnlyHint
                assert by_name['inspect_batch'].annotations.readOnlyHint
                assert not (by_name['exec_batch'].annotations and by_name['exec_batch'].annotations.readOnlyHint)
                assert 'Include already-known post-edit checks in command=' in by_name['apply_patch'].description
                for tool in by_name.values():
                    meta = tool.meta or {}
                    expected = RESOURCE_URI if tool.name in {'review_activity', 'apply_patch'} else None
                    assert meta.get('ui', {}).get('resourceUri') == expected
                    assert meta.get('openai/outputTemplate') == expected
                batch = (await client.read_resource(BATCH_RESOURCE_URI)).contents[0]
                assert batch.mimeType == 'text/html;profile=mcp-app'
                assert batch.meta['openai/widgetShowCodexWidgetInline'] is True
                assert 'ui/initialize' in batch.text
                resource = await client.read_resource(RESOURCE_URI)
                assert resource.contents[0].mimeType == 'text/html;profile=mcp-app'
                assert 'ui/initialize' in resource.contents[0].text
                assert resource.contents[0].meta['openai/widgetMinFrameHeight'] == 0
                assert resource.contents[0].meta['openai/widgetShowCodexWidgetInline'] is True
                response = await client.call_tool('workspace_tools', {'tool': 'workspace_info'},
                    meta={'threadId': 'local-install-smoke'})
                assert not response.isError
                info = json.loads(response.content[0].text)
                assert info['mode'] == 'local' and not info['bound']
                assert info['runtime']['version'] == version
                assert Path(info['runtime']['package_path']).resolve().is_relative_to(release)
                assert info['write_coordination'] == 'managed-files'
                assert 'operation_log' not in info
                assert not (Path(state) / 'resources.sqlite3').exists()
asyncio.run(check())
print('Installed command and diff card bindings and archived resources passed; no SSH was used.')
'''


def run(argv, **kwargs):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'Command failed: {argv[0]}')
    return result.stdout.strip()


@contextmanager
def update_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def current_release(root):
    current = root / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError('current must be a symbolic link; refusing to replace a directory')
    return current.resolve() if current.is_symlink() else None


def activate(root, release):
    if release.resolve().parent != (root / 'releases').resolve():
        raise ValueError('Release must be directly inside the installation releases directory')
    current_release(root)
    pending = root / ('.current-' + uuid.uuid4().hex)
    try:
        pending.symlink_to(release.relative_to(root))
        os.replace(pending, root / 'current')
    finally:
        pending.unlink(missing_ok=True)


def smoke(release, config, version):
    print('Checking installed MCP and inline resource...', flush=True)
    run([release / 'venv/bin/python', '-I', '-c', SMOKE, release, config, version], timeout=60)


def update(project, root, ref='stable'):
    project, root = Path(project).resolve(), Path(root).resolve()
    config = root / 'config/hosts.yaml'
    if not config.is_file():
        raise ValueError(f'Existing host configuration required: {config}')
    launcher = root / 'bin/remote-workbench-session'
    if not launcher.is_file() or str(root / 'current/venv/bin/remote-workbench-session') not in launcher.read_text():
        raise ValueError('The existing session launcher must execute current/venv/bin/remote-workbench-session')
    commit = run(['git', '-C', project, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}'])
    with update_lock(root):
        previous = current_release(root)
        if previous and (previous / 'release.json').is_file():
            installed = json.loads((previous / 'release.json').read_text())
            if installed.get('source_commit') == commit:
                smoke(previous, config, installed['version'])
                print(f'Already installed: {commit[:12]} ({previous})')
                return previous
        with tempfile.TemporaryDirectory(prefix='rwb-stable-source-') as temporary:
            temporary = Path(temporary)
            archive, source = temporary / 'source.tar', temporary / 'source'
            source.mkdir()
            run(['git', '-C', project, 'archive', '--format=tar', '-o', archive, commit])
            with tarfile.open(archive) as contents:
                contents.extractall(source, filter='data')
            version = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
            releases = root / 'releases'
            releases.mkdir(exist_ok=True)
            release = releases / f'{version}-{commit[:12]}'
            if release.exists():
                # Failed or historical attempts are retained, never overwritten.
                release = releases / f'{version}-{commit[:12]}-{uuid.uuid4().hex[:8]}'
            release.mkdir()
            print(f'Building fixed {ref} commit {commit[:12]} into {release}', flush=True)
            run(['uv', 'build', '--out-dir', release / 'dist', source])
            requirements = release / 'runtime-requirements.txt'
            run(['uv', 'export', '--project', source, '--frozen', '--no-dev', '--extra', 'images',
                 '--no-emit-project', '--no-hashes', '--output-file', requirements])
            wheels = list((release / 'dist').glob('*.whl'))
            if len(wheels) != 1:
                raise ValueError('Expected one fixed wheel')
            wheel = wheels[0]
            print('Installing locked runtime dependencies...', flush=True)
            run(['uv', 'venv', '--python', sys.executable, release / 'venv'])
            run(['uv', 'pip', 'install', '--python', release / 'venv/bin/python',
                 '--constraint', requirements, str(wheel) + '[images]'])
            smoke(release, config, version)
            manifest = {'version': version, 'source_commit': commit, 'source_ref': ref,
                        'wheel': str(wheel.relative_to(release)),
                        'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        'editable': False, 'smoke_test': 'passed; local only; no SSH',
                        'previous_release': str(previous) if previous else None,
                        'installed_at': datetime.now(timezone.utc).isoformat(),
                        'existing_codex_mcp_reload': 'required; not confirmed'}
            (release / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
            activate(root, release)
        print(f'Installed {version} at {commit[:12]}. Reload MCP to use it.', flush=True)
        return release


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=project)
    parser.add_argument('--install-root', type=Path, default=project / '.local/stable')
    parser.add_argument('--ref', default='stable', help='Existing local Git ref; default: stable')
    args = parser.parse_args()
    try:
        update(args.project, args.install_root, args.ref)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f'Update failed; current was not switched: {exc}\n')


if __name__ == '__main__':
    main()
