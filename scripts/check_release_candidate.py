#!/usr/bin/env python3
"""Validate a clean, fixed stable candidate; does not tag, publish or install."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def git(project, *args, check=True):
    return subprocess.run(['git', '-C', str(project), *args], capture_output=True,
                          text=True, check=check)


def validate(project, version, stable_ref='refs/remotes/origin/stable'):
    try:
        import tomllib
    except ImportError as exc:
        raise ValueError('Release validation requires Python 3.11+; the workflow uses 3.13') from exc
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('Use a final version X.Y.Z, without v, prerelease or dev suffix')
    project = Path(project).resolve()
    if Path(git(project, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != project:
        raise ValueError('Candidate must be the repository root')
    if git(project, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
        raise ValueError('Candidate working tree must be clean')
    actual = tomllib.loads((project / 'pyproject.toml').read_text())['project']['version']
    if actual != version:
        raise ValueError(f'Package version {actual!r} does not match requested release {version!r}')
    sha = git(project, 'rev-parse', 'HEAD').stdout.strip()
    if git(project, 'rev-parse', stable_ref).stdout.strip() != sha:
        raise ValueError('Candidate must be the exact fetched stable commit')
    tag = 'v' + version
    old = git(project, 'rev-parse', '--verify', '--quiet', 'refs/tags/' + tag + '^{}', check=False)
    if old.returncode == 0 and old.stdout.strip() != sha:
        raise ValueError('Existing release tag points to another commit; do not overwrite it')
    return {'commit': sha, 'version': version, 'tag': tag}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, required=True)
    p.add_argument('--version', required=True)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    try:
        result = validate(a.project, a.version)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        p.error(str(exc))
    if a.output:
        a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
