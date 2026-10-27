"""A Git submodule must not force a project snapshot during direct binding."""
import subprocess

from remote_workbench.session_server import create_server
from tests.test_session_server import call, router_at


async def test_bind_repository_with_gitlink_without_git_probe(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    child = root / 'roohs3toolkit'
    child.mkdir()
    (child / 'example.py').write_text('VALUE = 1\n')
    subprocess.run(['git', '-C', str(root), 'update-index', '--add',
                    '--cacheinfo', '160000,' + '1' * 40 + ',roohs3toolkit'], check=True)
    (root / '.gitmodules').write_text(
        '[submodule "roohs3toolkit"]\n\tpath = roohs3toolkit\n\turl = ./unused\n')

    class DirectoryExecutor:
        async def check_connection(self):
            return True, 'connected'

        async def run_program(self, argv, **kwargs):
            # Binding may inspect the directory, never enumerate Git files,
            # initialize submodules, or create a project copy.
            assert argv[:2] == ['python3', '-c']
            assert 'git' not in argv[2] and 'subprocess' not in argv[2]
            result = subprocess.run(argv, cwd=kwargs['cwd'], capture_output=True, text=True, check=True)
            return type('Result', (), {'stdout': result.stdout, 'exit_code': 0})()

    router = router_at(tmp_path / 'state')
    router.executor_factory = lambda *args, **kwargs: DirectoryExecutor()
    server = create_server(router)
    for selected in (root, child):
        bound = await call(server, 'submodule-chat', 'workspace_switch', host='a', cwd=str(selected))
        assert bound['bound'] and bound['directory_mode'] == 'direct'
        assert bound['cwd'] == str(selected.resolve())
        assert not bound['project_isolated']
    assert (child / 'example.py').read_text() == 'VALUE = 1\n'
    assert not list(tmp_path.rglob('.remote-workbench-sessions'))
