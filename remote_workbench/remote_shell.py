"""Shared stdlib-only login-shell launch code, also sent to remote Python."""
import base64
import os
import pwd
import shlex
import zlib


def login_shell():
    # Detached jobs keep the shell selected by their originating SSH command,
    # even when a startup file subsequently changes the SHELL variable.
    captured = os.environ.get("REMOTE_WORKBENCH_LOGIN_SHELL")
    if captured:
        return captured
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    except (KeyError, OSError):
        return os.environ.get("SHELL") or "/bin/sh"


def python_command(source):
    # A base64-only payload avoids history/quote expansion by the SSH account's
    # outer shell, including csh and fish. No user data is interpolated as code.
    data = source.encode("utf-8")
    # Worker source is nested inside the foreground supervisor command. Plain
    # base64 at each layer can exceed Linux's per-argument limit even when the
    # total argv is below ARG_MAX. Compress large source before encoding; stdin
    # remains available for the existing request/heartbeat protocol.
    if len(data) >= 16384:
        compressed = zlib.compress(data)
        if len(compressed) < len(data):
            encoded = base64.b64encode(compressed).decode("ascii")
            return "python3 -c " + shlex.quote(
                'import base64,zlib;exec(zlib.decompress(base64.b64decode("' + encoded + '")))')
    encoded = base64.b64encode(data).decode("ascii")
    return "python3 -c " + shlex.quote('import base64;exec(base64.b64decode("' + encoded + '"))')


def login_spec(command, cwd, preamble="", markers=None, trace=False, environment=None):
    shell = login_shell()
    name = os.path.basename(shell).lower()
    family = "csh" if name in {"csh", "tcsh"} else "fish" if name == "fish" else "sh"

    def quote(value):
        if family == "fish":
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return shlex.quote(value)

    parts = []
    if family == "csh":
        # Each request is a new session; internal literal paths must not be
        # interpreted as csh history substitutions.
        parts.append("set histchars = ''")
    if preamble:
        parts.append(preamble.strip())
    for key, value in (environment or {}).items():
        if not isinstance(key, str) or not key or not key.isascii() or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ValueError("Invalid shell environment name")
        if family == "csh":
            parts.append("setenv " + key + " " + quote(value))
        elif family == "fish":
            parts.append("set -gx " + key + " " + quote(value))
        else:
            parts.append("export " + key + "=" + quote(value))
    target = os.path.abspath(os.path.expanduser(cwd))
    if family == "csh":
        # exec is deliberate: tcsh can continue a -c command list after its
        # exit builtin. Never run user code when entering the project failed.
        stop = python_command("raise SystemExit(125)")
        parts.extend(["cd " + quote(target), "if ($status != 0) exec " + stop])
    else:
        parts.append("cd " + quote(target) + " || exit 125")
    if markers:
        code = "import os\n"
        for stream, descriptor in (("stdout", 1), ("stderr", 2)):
            marker = markers[stream].encode("utf-8")
            code += "os.write(" + str(descriptor) + ", " + repr(marker) + ".replace(b'%s', os.getcwdb(), 1))\n"
        parts.append(python_command(code))
    if trace:
        if family == "csh":
            parts.append("set echo")
        elif family == "fish":
            parts.append("set -g fish_trace 1")
        elif name == "zsh":
            parts.append("PS4='+ %N:%i: '; set -x")
        elif name == "bash":
            parts.append("PS4='+ ${BASH_SOURCE}:${LINENO}: '; BASH_XTRACEFD=2; set -x")
        else:
            parts.append("PS4='+ '; set -x")
    parts.append(command)
    env_updates = {"SHELL": shell, "REMOTE_WORKBENCH_LOGIN_SHELL": shell}
    if not os.environ.get("TERM"):
        env_updates["TERM"] = "dumb"
    # OpenSSH marks login shells through argv[0], rather than requiring a
    # shell-specific '-l' option. -i loads the interactive startup configuration;
    # no PTY is allocated, preserving the tool's stdin bytes.
    return {"shell": shell, "argv": ["-" + os.path.basename(shell), "-i", "-c", "\n".join(parts)],
            "env_updates": env_updates}
