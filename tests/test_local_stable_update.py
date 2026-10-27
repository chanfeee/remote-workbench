"""Exercise update ordering and failure recovery without package downloads."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

if sys.version_info < (3, 11):
    pytest.skip('Local stable updater requires Python 3.11+', allow_module_level=True)

spec = importlib.util.spec_from_file_location('update_local_stable',
    Path(__file__).resolve().parents[1] / 'scripts/update_local_stable.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    project, root = tmp_path / 'project', tmp_path / 'install'
    project.mkdir()
    (project / 'pyproject.toml').write_text('[project]\nname="example"\nversion="1.2.3"\n')
    for argv in (['git', 'init', '-q'], ['git', 'add', '.'],
                 ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source'],
                 ['git', 'branch', 'stable']):
        subprocess.run(argv, cwd=project, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'stable'], cwd=project, text=True).strip()
    # The installed source must come from stable, never the dirty checkout.
    (project / 'pyproject.toml').write_text('[project]\nversion="9.9.9"\n')
    (root / 'config').mkdir(parents=True)
    (root / 'config/hosts.yaml').write_text('hosts: {}\n')
    (root / 'session-data').mkdir()
    (root / 'session-data/keep').write_text('existing state')
    (root / 'bin').mkdir()
    (root / 'bin/remote-workbench-session').write_text(str(root / 'current/venv/bin/remote-workbench-session'))
    old = root / 'releases/old'
    old.mkdir(parents=True)
    (old / 'release.json').write_text(json.dumps({'source_commit': 'old', 'version': '1.0.0'}))
    (root / 'current').symlink_to('releases/old')
    real_run = updater.run
    calls = []

    def run(argv, **kwargs):
        args = [str(x) for x in argv]
        calls.append(args)
        if args[0] != 'uv':
            return real_run(argv, **kwargs)
        if args[1] == 'build':
            # Assert on the actual archived input, rather than duplicating the implementation.
            source = Path(args[-1])
            assert '1.2.3' in (source / 'pyproject.toml').read_text()
            output = Path(args[args.index('--out-dir') + 1])
            output.mkdir()
            (output / 'example.whl').write_bytes(b'fixed wheel')
        elif args[1] == 'export':
            Path(args[args.index('--output-file') + 1]).write_text('dependency==1\n')
        return ''

    def smoke(release, config, version):
        assert (root / 'current').resolve() == old  # Switching must follow verification.
        assert version == '1.2.3'
        calls.append(['smoke', str(release)])

    monkeypatch.setattr(updater, 'run', run)
    monkeypatch.setattr(updater, 'smoke', smoke)
    return project, root, old, commit, calls


def test_fixed_ref_build_then_verified_switch_preserves_dirty_source_and_state(installation):
    project, root, old, commit, calls = installation
    installed = updater.update(project, root)
    assert (root / 'current').resolve() == installed
    manifest = json.loads((installed / 'release.json').read_text())
    assert manifest['source_commit'] == commit and manifest['previous_release'] == str(old)
    assert manifest['version'] == '1.2.3' and not manifest['editable']
    assert '9.9.9' in (project / 'pyproject.toml').read_text()
    assert (root / 'session-data/keep').read_text() == 'existing state'
    assert (root / 'config/hosts.yaml').read_text() == 'hosts: {}\n'
    assert old.is_dir()
    assert calls[-1][0] == 'smoke'


@pytest.mark.parametrize('failure', ['build', 'install', 'smoke'])
def test_failed_update_keeps_current_and_retains_candidate_for_inspection(installation, monkeypatch, failure):
    project, root, old, _, _ = installation
    real_run = updater.run

    def run(argv, **kwargs):
        args = [str(x) for x in argv]
        if args[:2] == ['uv', 'build'] and failure == 'build':
            raise RuntimeError('build failed')
        if args[:3] == ['uv', 'pip', 'install'] and failure == 'install':
            raise RuntimeError('install failed')
        return real_run(argv, **kwargs)

    monkeypatch.setattr(updater, 'run', run)
    if failure == 'smoke':
        def fail(*args):
            raise RuntimeError('smoke failed')
        monkeypatch.setattr(updater, 'smoke', fail)
    with pytest.raises(RuntimeError, match='failed'):
        updater.update(project, root)
    assert (root / 'current').resolve() == old
    assert (root / 'session-data/keep').read_text() == 'existing state'
    assert len(list((root / 'releases').iterdir())) == 2


def test_already_installed_commit_is_verified_without_rebuilding(installation, monkeypatch):
    project, root, old, commit, calls = installation
    (old / 'release.json').write_text(json.dumps({'source_commit': commit, 'version': '1.2.3'}))
    assert updater.update(project, root) == old
    assert not any(call[0] == 'uv' for call in calls)
    assert calls[-1][0] == 'smoke'


def test_activation_rejects_external_directory_and_nonlink_current(tmp_path):
    root = tmp_path / 'install'
    root.mkdir()
    with pytest.raises(ValueError, match='inside'):
        updater.activate(root, tmp_path / 'external')
    candidate = root / 'releases/new'
    candidate.mkdir(parents=True)
    (root / 'current').mkdir()
    with pytest.raises(ValueError, match='symbolic link'):
        updater.activate(root, candidate)
    assert (root / 'current').is_dir()
