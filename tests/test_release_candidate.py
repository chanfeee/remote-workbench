"""Release checks use only disposable local Git repositories."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_release_candidate', ROOT/'scripts/check_release_candidate.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def git(path, *args):
    return subprocess.check_output(['git','-C',str(path),*args],text=True).strip()


@pytest.fixture
def candidate(tmp_path):
    (tmp_path/'pyproject.toml').write_text('[project]\nname="example"\nversion="1.2.3"\n')
    git(tmp_path,'init','-q')
    git(tmp_path,'add','.')
    git(tmp_path,'-c','user.name=Release Test','-c','user.email=release@example.invalid','commit','-qm','candidate')
    git(tmp_path,'update-ref','refs/remotes/origin/stable','HEAD')
    return tmp_path


@pytest.mark.skipif(sys.version_info < (3,11),reason='Release tooling uses workflow Python 3.13')
def test_clean_fixed_stable_candidate(candidate):
    value=release.validate(candidate,'1.2.3')
    assert value=={'commit':git(candidate,'rev-parse','HEAD'),'version':'1.2.3','tag':'v1.2.3'}
    git(candidate,'tag','v1.2.3')
    assert release.validate(candidate,'1.2.3')==value


@pytest.mark.skipif(sys.version_info < (3,11),reason='Release tooling uses workflow Python 3.13')
@pytest.mark.parametrize('version',['v1.2.3','1.2.3a1','1.2.3.dev1','1.2','01.2.3','1.2.3;touch marker'])
def test_invalid_release_versions(candidate,version):
    with pytest.raises(ValueError,match='final version'):release.validate(candidate,version)
    assert not (candidate/'marker').exists()


@pytest.mark.skipif(sys.version_info < (3,11),reason='Release tooling uses workflow Python 3.13')
def test_version_mismatch_and_dirty_sources(candidate):
    with pytest.raises(ValueError,match='does not match'):release.validate(candidate,'1.2.4')
    (candidate/'untracked.txt').write_text('not released')
    with pytest.raises(ValueError,match='clean'):release.validate(candidate,'1.2.3')


@pytest.mark.skipif(sys.version_info < (3,11),reason='Release tooling uses workflow Python 3.13')
def test_wrong_stable_commit_and_tag_collision(candidate):
    git(candidate,'tag','v1.2.3')
    (candidate/'README').write_text('changed')
    git(candidate,'add','README')
    git(candidate,'-c','user.name=Release Test','-c','user.email=release@example.invalid','commit','-qm','next')
    with pytest.raises(ValueError,match='exact fetched stable'):release.validate(candidate,'1.2.3')
    git(candidate,'update-ref','refs/remotes/origin/stable','HEAD')
    with pytest.raises(ValueError,match='do not overwrite'):release.validate(candidate,'1.2.3')


def test_workflows_use_main_stable_and_manual_drafts_only():
    ci=yaml.load((ROOT/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
    assert ci['on']['push']['branches']==['main','stable']
    assert ci['on']['pull_request']['branches']==['main','stable']
    workflow=yaml.load((ROOT/'.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(workflow['on'])=={'workflow_dispatch'}
    assert workflow['jobs']['release']['if']=="github.ref == 'refs/heads/main'"
    steps=workflow['jobs']['release']['steps']
    publish=steps[-1]['run']
    assert '--draft' in publish and '--target "$COMMIT"' in publish
    assert steps[1]['with']['ref']=='stable' and steps[1]['with']['fetch-depth']=='0'
    assert any('Recheck' in step['name'] for step in steps)
