import ast
import os
from pathlib import Path
import subprocess

import pytest

from remote_workbench.native_patch import compile_native_patch
from remote_workbench import audit
from remote_workbench.tools.patch import ssh_apply_patch


def git(root, *args, data=None):
    return subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, check=True)


@pytest.mark.parametrize("before,body,after", [
    ("one\ntwo\n", "@@\n one\n-two\n+三", "one\n三\n"),
    ("one\ntwo", "@@\n one\n-two\n+three", "one\nthree"),
    ("one\r\ntwo\r\n", "@@\n one\n-two\n+three", "one\r\nthree\r\n"),
    ("one\n", "@@\n+two\n*** End of File", "one\ntwo\n"),
    ("a\nb\nc\nd\n", "@@\n-a\n+A\n@@\n-c\n+C", "A\nb\nC\nd\n"),
    ("first:\nsame\nsecond:\nsame\n", "@@ second:\n-same\n+new", "first:\nsame\nsecond:\nnew\n"),
    ("one\n\ntwo\n", "@@\n one\n\n-two\n+three", "one\n\nthree\n"),
])
def test_context_updates_compile_to_applicable_git_patch(tmp_path, before, body, after):
    git(tmp_path, "init", "-q")
    path = tmp_path / 'a "quoted" name.txt'
    path.write_bytes(before.encode())
    patch = compile_native_patch('*** Begin Patch\n*** Update File: a "quoted" name.txt\n' + body + '\n*** End Patch', tmp_path)
    git(tmp_path, "apply", "--check", "-", data=patch.encode())
    assert path.read_bytes() == before.encode()
    git(tmp_path, "apply", "-", data=patch.encode())
    assert path.read_bytes() == after.encode()


def test_add_move_delete_and_empty_files_are_one_checked_patch(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "old").write_text("before\n")
    (tmp_path / "old").chmod(0o755)
    (tmp_path / "remove").write_text("")
    patch = compile_native_patch("""*** Begin Patch
*** Update File: old
*** Move to: sub/new
@@
-before
+after
*** Delete File: remove
*** Add File: added
+value
*** Add File: empty
*** End Patch
""", tmp_path)
    git(tmp_path, "apply", "--check", "-", data=patch.encode())
    git(tmp_path, "apply", "-", data=patch.encode())
    assert not (tmp_path / "old").exists() and not (tmp_path / "remove").exists()
    assert (tmp_path / "sub/new").read_text() == "after\n"
    assert (tmp_path / "sub/new").stat().st_mode & 0o111
    assert (tmp_path / "added").read_text() == "value\n"
    assert (tmp_path / "empty").read_bytes() == b""


@pytest.mark.parametrize("path", ["../outside", ".git/config", ".GIT/config", "sub/../../outside", "/etc/profile"])
def test_native_paths_cannot_escape_or_edit_git(tmp_path, path):
    with pytest.raises(ValueError, match="path"):
        compile_native_patch(f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch", tmp_path)


def test_symlinks_nonregular_files_and_oversized_content_are_rejected(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError, match="symbolic"):
        compile_native_patch("*** Begin Patch\n*** Add File: link/out\n+x\n*** End Patch", tmp_path)
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ValueError, match="regular"):
        compile_native_patch("*** Begin Patch\n*** Delete File: fifo\n*** End Patch", tmp_path)
    with pytest.raises(ValueError, match="size limit"):
        compile_native_patch("*** Begin Patch\n*** Add File: new\n+large\n*** End Patch", tmp_path, 2)


@pytest.mark.parametrize("body,match", [
    ("@@\n-same\n+new", "ambiguous"),
    ("@@\n-missing\n+new", "missing"),
    ("@@\n+new", "context"),
    ("wrong", "Expected @@"),
])
def test_invalid_hunks_fail_without_changing_any_file(tmp_path, body, match):
    path = tmp_path / "value"
    path.write_text("same\nsame\n")
    with pytest.raises(ValueError, match=match):
        compile_native_patch("*** Begin Patch\n*** Add File: new\n+new\n*** Update File: value\n" + body + "\n*** End Patch", tmp_path)
    assert not (tmp_path / "new").exists()
    assert path.read_text() == "same\nsame\n"


async def test_real_worker_checks_and_applies_context_patch(local):
    _, root = local
    git(root, "init", "-q")
    path = root / "code.py"
    path.write_text("answer = 1\n")
    text = "*** Begin Patch\n*** Update File: code.py\n@@\n-answer = 1\n+answer = 2\n*** End Patch"
    checked = await ssh_apply_patch(text, cwd=str(root), check_only=True)
    assert checked.get("success"), checked
    assert path.read_text() == "answer = 1\n"
    changed = await ssh_apply_patch(text, cwd=str(root))
    assert changed.get("success"), changed
    assert path.read_text() == "answer = 2\n"
    assert "normalized_patch" not in changed and "diff" not in changed
    saved_diff = (audit.directory(changed["output_id"]) / "diff").read_text()
    assert "@@ -1 +1 @@" in saved_diff and "+answer = 2" in saved_diff
    assert "*** Begin Patch" not in saved_diff


def test_worker_compiler_remains_python39_compatible():
    path = Path(__file__).parents[1] / "remote_workbench/native_patch.py"
    ast.parse(path.read_text(), feature_version=(3, 9))
