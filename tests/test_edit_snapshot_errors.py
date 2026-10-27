"""Distinguish expected snapshot failures without creating or following paths."""
import pytest

from remote_workbench import remote_worker
from remote_workbench.tools.files import ssh_read_file


def read(path):
    return remote_worker.read_lines({'path': str(path), 'for_edit': True,
                                    'offset': 0, 'limit': 20, 'budget': 4096})


def test_missing_edit_file_explains_creation_without_creating(tmp_path):
    path = tmp_path / 'new directory' / 'approval.json'
    result = read(path)
    assert result['error'] == 'file_not_found'
    assert result['path'] == str(path)
    assert 'Add File' in result['message'] and 'write_file' in result['message']
    assert not path.parent.exists()


def test_directory_is_not_reported_as_missing_or_symlink(tmp_path):
    assert read(tmp_path)['error'] == 'not_regular_file'


@pytest.mark.parametrize('kind', ['file', 'parent', 'broken'])
def test_symlinks_are_identified_and_not_followed(tmp_path, kind):
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'value').write_text('unchanged\n')
    link = tmp_path / 'link'
    link.symlink_to(target if kind == 'parent' else target / ('missing' if kind == 'broken' else 'value'))
    path = link / 'value' if kind == 'parent' else link
    result = read(path)
    assert result['error'] == 'edit_symlink_path'
    assert result['symlink_component'] == str(link)
    assert (target / 'value').read_text() == 'unchanged\n'


def test_missing_git_metadata_is_still_protected(tmp_path):
    assert read(tmp_path / '.git' / 'config')['error'] == 'protected_git_path'


async def test_missing_edit_file_survives_worker_protocol_without_raw_marker(local):
    _, root = local
    result = await ssh_read_file(str(root / 'missing.json'), for_edit=True, local_copy=False)
    assert result['error'] == 'file_not_found'
    assert 'REMOTE_WORKBENCH_RESULT_' not in result['message']
    assert 'edit_id' not in result
    assert not (root / 'missing.json').exists()
