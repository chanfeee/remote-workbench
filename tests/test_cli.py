"""Manual SSH entry point: terminal handoff, config selection and mux parity."""

import json
import os
import shutil
import subprocess
import sys

import pytest
import yaml

from remote_workbench import cli
from remote_workbench.config import load_config
from remote_workbench.ssh import SSHExecutor


@pytest.fixture
def host_file(tmp_path):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({
        "default_host": "cluster",
        "hosts": {"cluster": {
            "ssh_host": "cluster-alias", "user": "alice", "port": 2222,
            "control_path": str(tmp_path / "ctrl-%r@%h:%p"),
        }},
    }))
    return path


@pytest.mark.parametrize("mode", [[], ["--background"], ["--check"]])
def test_cli_hands_terminal_and_exit_status_to_ssh(tmp_path, host_file, mode):
    # A real subprocess replaces itself with a fake ssh executable. Its stdin,
    # stdout, stderr and nonzero exit code must reach the caller unchanged.
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
        "print(input())\n"
        "print('ssh diagnostic', file=sys.stderr)\n"
        "sys.exit(23)\n"
    )
    ssh.chmod(0o700)
    result = subprocess.run(
        [sys.executable, "-m", "remote_workbench.cli", "establish", "cluster",
         "--config", str(host_file), *mode],
        input="terminal input\n", capture_output=True, text=True, timeout=10,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 23
    arguments, terminal_input = result.stdout.splitlines()
    argv = json.loads(arguments)
    assert terminal_input == "terminal input"
    assert result.stderr == "ssh diagnostic\n"
    host = load_config(host_file).get_host()
    assert argv[:2] == ["-S", host.resolved_control_path]
    assert argv[-3:] == ["-p", "2222", "alice@cluster-alias"]
    if mode == ["--check"]:
        assert argv[2:4] == ["-O", "check"]
        assert "-M" not in argv
    else:
        assert argv[2] == "-M"
        assert ("-f" in argv and "-N" in argv) == bool(mode)


@pytest.mark.parametrize("selection", ["default", "only", "env"])
def test_omitted_host_and_config_selection(host_file, monkeypatch, selection):
    if selection == "only":
        raw = yaml.safe_load(host_file.read_text())
        raw.pop("default_host")
        host_file.write_text(yaml.safe_dump(raw))
    captured = []
    monkeypatch.setattr(cli.os, "execvp", lambda executable, argv: captured.append(argv))
    if selection == "env":
        monkeypatch.setenv("REMOTE_WORKBENCH_CONFIG", str(host_file))
        cli._exec_establish([])
    else:
        cli._exec_establish([f"--config={host_file}"])
    assert captured[0][-1] == "alice@cluster-alias"


def test_establish_and_mcp_resolve_alias_to_same_socket(host_file, tmp_path, monkeypatch):
    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("OpenSSH is needed for config expansion verification")
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host cluster-alias\n    HostName real.example.invalid\n")
    captured = []
    monkeypatch.setattr(cli.os, "execvp", lambda executable, argv: captured.append(argv))
    cli._exec_establish(["cluster", "--config", str(host_file)])
    host = load_config(host_file).get_host()
    executor = SSHExecutor(host)
    paths = []
    for argv in (captured[0], executor._ssh_args("true")):
        # -G only evaluates local config; it never connects or authenticates.
        result = subprocess.run([ssh, "-G", "-F", str(ssh_config), *argv[1:]],
                                capture_output=True, text=True, check=True, timeout=5)
        paths.append(next(line.split(" ", 1)[1] for line in result.stdout.splitlines()
                          if line.startswith("controlpath ")))
    assert paths == [host.resolved_control_path] * 2
    assert "cluster-alias" in paths[0]


@pytest.mark.parametrize("arguments", [
    ["--background", "--check"], ["--config"], ["--unknown"], ["one", "two"],
])
def test_bad_arguments_do_not_run_ssh(monkeypatch, arguments):
    def unexpected(*args):
        pytest.fail("invalid arguments must not run SSH")
    monkeypatch.setattr(cli.os, "execvp", unexpected)
    with pytest.raises(SystemExit) as error:
        cli._exec_establish(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("content", ["[invalid yaml", "hosts: {}", "hosts: []"])
def test_bad_config_reports_error_without_traceback(tmp_path, capsys, content):
    config = tmp_path / "bad.yaml"
    config.write_text(content)
    with pytest.raises(SystemExit) as error:
        cli._exec_establish(["--config", str(config)])
    assert error.value.code == 1
    assert "remote-workbench establish:" in capsys.readouterr().err


def test_missing_ssh_reports_actionable_error(host_file, monkeypatch, capsys):
    def missing(*args):
        raise FileNotFoundError("ssh not found")
    monkeypatch.setattr(cli.os, "execvp", missing)
    with pytest.raises(SystemExit) as error:
        cli._exec_establish(["--config", str(host_file)])
    assert error.value.code == 1
    assert "could not run ssh" in capsys.readouterr().err
