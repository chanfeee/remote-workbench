"""Keep real worker envelopes below Linux's single-argument size limit."""
import asyncio
import errno
import os
import shlex
import subprocess

import pytest

from remote_workbench.config import HostConfig
from remote_workbench.remote_client import SOURCE
from remote_workbench.remote_shell import python_command
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import files


@pytest.mark.parametrize("comment_lines", [1, 10000])
def test_python_source_transport_preserves_unicode_and_binary_stdin(comment_lines):
    source = "# 测试源码 ' \" $ !\n" * comment_lines
    source += "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    command = python_command(source)
    assert len(command.encode()) < 128 * 1024
    payload = b"binary\x00stdin\n\xff\xe4\xbd\xa0\xe5\xa5\xbd"
    result = subprocess.run(shlex.split(command), input=payload,
                            capture_output=True, timeout=10, check=True)
    assert result.stdout == payload
    assert result.stderr == b""


def test_nested_production_worker_command_fits_linux_single_argument_limit(tmp_path):
    executor = SSHExecutor(HostConfig(ssh_host="simulated", default_cwd=str(tmp_path),
                                     execution_enabled=True))
    command = executor._wrap_command(python_command(SOURCE), str(tmp_path), "a" * 32)
    # Linux MAX_ARG_STRLEN is 32 pages (128 KiB on standard 4 KiB pages).
    # Check the final outer command, not only the innermost Python payload.
    assert len(command.encode()) + 1 < 128 * 1024


async def test_managed_worker_roundtrip_with_linux_argument_limit(local, monkeypatch):
    _, root = local
    simulated_transport = asyncio.create_subprocess_exec

    async def limited_transport(*arguments, **kwargs):
        if any(len(os.fsencode(value)) + 1 > 128 * 1024 for value in arguments):
            raise OSError(errno.E2BIG, "Argument list too long")
        return await simulated_transport(*arguments, **kwargs)

    monkeypatch.setattr("remote_workbench.ssh.asyncio.create_subprocess_exec", limited_transport)
    written = await files.ssh_write_file(path="unicode.txt", content="你好\n")
    assert written.get("success") is True, written
    read = await files.ssh_read_file(path="unicode.txt")
    assert read.get("content") == "1\t你好", read
    assert (root / "unicode.txt").read_text() == "你好\n"
