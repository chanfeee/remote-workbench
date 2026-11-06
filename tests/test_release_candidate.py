"""Release checks use only disposable local Git repositories."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
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


@pytest.mark.parametrize('ref,version', [
    ('refs/tags/v0.4.5','0.4.5'), ('refs/tags/v1.2.3','1.2.3'),
])
def test_explicit_version_tag(ref,version):
    assert release.version_from_tag(ref)==version


@pytest.mark.parametrize('ref', [
    'refs/heads/v0.4.5','v0.4.5','refs/tags/v5','refs/tags/v01.2.3',
    'refs/tags/v1.2.3rc1','refs/tags/v1.2.3;touch marker',
])
def test_non_release_refs_are_rejected(ref):
    with pytest.raises(ValueError,match='explicit version tag'):
        release.version_from_tag(ref)


def test_workflows_keep_manual_drafts_and_publish_only_explicit_tags():
    ci=yaml.load((ROOT/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
    assert ci['on']['push']['branches']==['main','stable']
    assert ci['on']['pull_request']['branches']==['main','stable']
    workflow=yaml.load((ROOT/'.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(workflow['on'])=={'workflow_dispatch','push'}
    assert workflow['on']['push']=={'tags':['v*']}
    condition=workflow['jobs']['release']['if']
    assert "github.ref == 'refs/heads/main'" in condition
    assert "startsWith(github.ref, 'refs/tags/v')" in condition
    steps=workflow['jobs']['release']['steps']
    publish=steps[-1]['run']
    assert '--draft' in publish and '--target "$COMMIT"' in publish
    assert '--verify-tag' in publish and '--notes-file' in publish
    assert '"$RELEASE_EVENT" = "push"' in publish
    assert "github.ref || 'stable'" in steps[1]['with']['ref']
    assert steps[1]['with']['fetch-depth']=='0'
    assert any('Recheck' in step['name'] for step in steps)


@pytest.mark.skipif(sys.version_info < (3,11),reason='Release tooling uses workflow Python 3.13')
def test_tag_cli_keeps_candidate_and_version_checks(candidate,tmp_path):
    result=subprocess.run([sys.executable,str(ROOT/'scripts/check_release_candidate.py'),
        '--project',str(candidate),'--tag-ref','refs/tags/v1.2.3'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert '"tag": "v1.2.3"' in result.stdout
    rejected=subprocess.run([sys.executable,str(ROOT/'scripts/check_release_candidate.py'),
        '--project',str(candidate),'--tag-ref','refs/heads/v1.2.3'],capture_output=True,text=True)
    assert rejected.returncode!=0 and 'explicit version tag' in rejected.stderr


def test_release_shell_blocks_parse(tmp_path):
    workflow=yaml.load((ROOT/'.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    for index,step in enumerate(workflow['jobs']['release']['steps']):
        if 'run' not in step:continue
        script=tmp_path/f'step-{index}.sh'
        script.write_text(step['run'])
        subprocess.run(['bash','-n',str(script)],check=True,capture_output=True,text=True)


@pytest.mark.skipif(sys.version_info < (3,11),reason='Release tooling uses workflow Python 3.13')
@pytest.mark.parametrize('event',['push','workflow_dispatch'])
def test_workflow_executes_fixed_candidate_checks(candidate,tmp_path_factory,event):
    runner=tmp_path_factory.mktemp('release-workflow')
    (runner/'candidate').symlink_to(candidate,target_is_directory=True)
    scripts=runner/'release-tools/scripts';scripts.mkdir(parents=True)
    shutil.copy2(ROOT/'scripts/check_release_candidate.py',scripts/'check_release_candidate.py')
    scratch=runner/'scratch';scratch.mkdir()
    env={**os.environ,'RUNNER_TEMP':str(scratch),'RELEASE_EVENT':event,
         'RELEASE_REF':'refs/tags/v1.2.3','RELEASE_VERSION':'1.2.3',
         'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','')}
    workflow=yaml.load((ROOT/'.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    steps=workflow['jobs']['release']['steps']
    initial=next(s['run']for s in steps if s.get('name')=='Validate immutable candidate and version')
    recheck=next(s['run']for s in steps if s.get('name')=='Recheck tested sources before building')
    subprocess.run(['bash','-e','-c',initial],cwd=runner,env=env,check=True,capture_output=True,text=True)
    saved=json.loads((scratch/'release-candidate.json').read_text())
    assert saved['commit']==git(candidate,'rev-parse','HEAD')
    assert saved['version']=='1.2.3'
    subprocess.run(['bash','-e','-c',recheck],cwd=runner,env=env,check=True,capture_output=True,text=True)
    (candidate/'pyproject.toml').write_text('changed after testing')
    rejected=subprocess.run(['bash','-e','-c',recheck],cwd=runner,env=env,capture_output=True,text=True)
    assert rejected.returncode!=0 and 'clean' in rejected.stderr


@pytest.mark.parametrize('event,draft',[('push',False),('workflow_dispatch',True)])
def test_publication_mode_and_fixed_target(tmp_path,event,draft):
    candidate=tmp_path/'candidate';dist=candidate/'dist';dist.mkdir(parents=True)
    assets=['package.whl','package.tar.gz','runtime-requirements.txt','SHA256SUMS']
    for name in assets:(dist/name).write_text('fixture')
    notes=candidate/'docs/releases/v1.2.3.md';notes.parent.mkdir(parents=True);notes.write_text('Release notes')
    scratch=tmp_path/'scratch';scratch.mkdir()
    commit='a'*40
    (scratch/'release-candidate.json').write_text(json.dumps({'tag':'v1.2.3','commit':commit}))
    bin_dir=tmp_path/'bin';bin_dir.mkdir();record=tmp_path/'gh-arguments.json'
    gh=bin_dir/'gh'
    gh.write_text(f'#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\np=Path(os.environ["RELEASE_TEST_ARGUMENTS"])\ncalls=json.loads(p.read_text()) if p.exists() else []\ncalls.append(sys.argv[1:])\np.write_text(json.dumps(calls))\nif os.environ.get("RELEASE_TEST_FAIL_CREATE")=="1" and sys.argv[1:3]==["release","create"]: sys.exit(7)\n')
    gh.chmod(0o700)
    env={**os.environ,'RUNNER_TEMP':str(scratch),'RELEASE_EVENT':event,
         'RELEASE_TEST_ARGUMENTS':str(record),
         'PATH':str(bin_dir)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','')}
    workflow=yaml.load((ROOT/'.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    publish=workflow['jobs']['release']['steps'][-1]['run']
    subprocess.run(['bash','-e','-c',publish],cwd=tmp_path,env=env,check=True,capture_output=True,text=True)
    calls=json.loads(record.read_text());args=calls[0]
    assert args[:3]==['release','create','v1.2.3']
    assert args[args.index('--target')+1]==commit
    assert set('candidate/dist/'+name for name in assets)<=set(args)
    assert '--draft'in args
    assert ('--verify-tag'in args)is(not draft)
    if draft:
        assert len(calls)==1
    else:
        assert args[args.index('--notes-file')+1]=='candidate/docs/releases/v1.2.3.md'
        assert calls[1]==['release','edit','v1.2.3','--draft=false','--latest']
        assert len(calls)==2
    record.unlink()
    failed=subprocess.run(['bash','-e','-c',publish],cwd=tmp_path,
        env={**env,'RELEASE_TEST_FAIL_CREATE':'1'},capture_output=True,text=True)
    assert failed.returncode==7
    assert len(json.loads(record.read_text()))==1
