import json
from types import SimpleNamespace
import pytest

from remote_workbench import audit
from remote_workbench import remote_client
from remote_workbench.remote_client import edit


@pytest.mark.parametrize("check_only", [False, True])
async def test_submitted_patch_is_local_viewer_stream_without_model_echo(local, check_only):
    executor, root = local
    await executor.run("git init -q")
    (root / "example").write_text("before\n")
    patch = "--- a/example\n+++ b/example\n@@ -1 +1 @@\n-before\n+after\n"
    result = await edit(executor, {"op": "apply_patch", "patch": patch, "check_only": check_only})
    assert result["success"]
    assert "diff" not in result
    assert 'execution_state' not in result and 'timed_out' not in result
    directory = audit.directory(result["output_id"])
    assert (directory / "diff").read_text() == patch
    record = json.loads((directory / "record.json").read_text())
    assert record["diff_kind"] == "submitted_patch"
    assert record["patch_result"] == ("checked" if check_only else "applied")
    assert (root / "example").read_text() == ("before\n" if check_only else "after\n")


async def test_failed_patch_is_labelled_as_failed_in_human_record(local):
    executor, root = local
    await executor.run("git init -q")
    (root / "example").write_text("different\n")
    patch = "--- a/example\n+++ b/example\n@@ -1 +1 @@\n-before\n+after\n"
    result = await edit(executor, {"op": "apply_patch", "patch": patch})
    assert result["error"] == "patch_conflict"
    record = json.loads((audit.directory(result["output_id"]) / "record.json").read_text())
    assert record["patch_result"] == "failed" and record["diff_kind"] == "submitted_patch"
    assert (root / "example").read_text() == "different\n"


async def test_interrupted_patch_still_saves_submitted_diff(tmp_path, monkeypatch):
    patch = '--- a/example\n+++ b/example\n@@ -1 +1 @@\n-before\n+after\n'
    with audit.scoped_root(tmp_path):
        output_id = audit.create(kind='command')

        async def interrupted(*args, **kwargs):
            return SimpleNamespace(output_id=output_id, exit_code=255, stderr='connection lost',
                                   execution_state='remote_state_unknown')

        monkeypatch.setattr(remote_client, 'request', interrupted)
        result = await edit(None, {'op': 'apply_patch', 'patch': patch})
        assert result['error'] == 'remote_failed' and result['execution_state'] == 'remote_state_unknown'
        assert audit.read_output(result['output_id'], 'diff')['content'] == patch
        record = json.loads((audit.directory(output_id) / 'record.json').read_text())
        assert record['patch_result'] == 'unconfirmed'
