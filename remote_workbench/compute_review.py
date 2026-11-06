"""Lightweight review prompts, not a shell sandbox or a resource limiter.

Common parallel launchers are caught as a backstop. The agent must also review
scripts whose fan-out cannot be inferred from their command line.
"""
import os
import re
import shlex

from remote_workbench import audit


REVIEW_INSTRUCTIONS = (
    " Before substantial computation, batch submission, high parallelism, or tests of runtime/spawn wrappers, "
    "show the exact command, expected concurrency, host/cwd and environment to the user and wait for explicit confirmation. "
    "Then pass compute_review={concurrency:...,environment:...,confirmation:...}. "
    "Never invent user confirmation or treat generic permission to test as approval of a compute plan. "
    "Inspect scripts when fan-out is unclear. Ordinary file operations and small tests run directly. "
    "Review is an agent workflow; command-line detection is incomplete and imposes no OS resource cap. "
)


def compute_risks(command, threshold=8):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ["Command syntax could not be assessed"]
    segments, part = [], []
    for token in tokens:
        if token and all(char in ";&|()\n" for char in token):
            if part:
                segments.append(part)
                part = []
        else:
            part.append(token)
    if part:
        segments.append(part)
    risks = []
    for words in segments:
        words = list(words)
        while words and (words[0] in {"exec", "env", "command", "nohup", "time"} or
                         re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0])):
            word = words.pop(0)
            if re.match(r"LD_(PRELOAD|AUDIT)=", word):
                risks.append("Runtime/preload wrapper requires review")
        if not words:
            continue
        program = os.path.basename(words[0])
        if (program.startswith("python") and "-m" in words and
                ("-c" not in words or words.index("-m") < words.index("-c"))):
            module_index = words.index("-m") + 1
            if module_index < len(words):
                words = [words[module_index], *words[module_index + 1:]]
                program = words[0]
        # A flag passed to a workload/script is not the launcher's own query:
        # python -c CODE --help still executes CODE; mpirun app --help runs app.
        if len(words) == 2 and words[1] in {"--help", "--version", "-h"}:
            continue
        if program in {"mpirun", "mpiexec", "srun", "sbatch", "qsub", "bsub", "condor_submit", "parallel", "torchrun"}:
            risks.append(program + " launches or submits a compute workload")
        options = []
        if program in {"make", "gmake", "ninja", "cmake", "ctest", "pytest", "py.test", "bazel", "xargs"}:
            flags = (r"-n|--numprocesses=?" if program in {"pytest", "py.test"} else
                     r"-P|--max-procs=?" if program == "xargs" else r"-j|--jobs=?|--parallel=?")
            for i, word in enumerate(words[1:], 1):
                match = re.fullmatch(r"(?:" + flags + r")([0-9]+|auto|all)?", word)
                if match:
                    value = match[1]
                    if value is None and i + 1 < len(words):
                        value = words[i + 1] if re.fullmatch(r"[0-9]+|auto|all", words[i + 1]) else None
                    options.append(value)
            if program == "ninja" and not options and "-n" not in words:
                options.append(None)
        for value in options:
            if value is None or not value.isdecimal() or int(value) == 0 or int(value) >= threshold:
                risks.append("Parallelism requires review: " + (value or "automatic/unbounded"))
        if program.startswith("python") and "-c" in words:
            for count in re.findall(r"(?:max_workers|processes)\s*=\s*([0-9]+)", words[words.index("-c") + 1]):
                if int(count) >= threshold:
                    risks.append("Python worker count requires review: " + count)
    return list(dict.fromkeys(risks))


def check_compute_review(command, cwd, host, review, threshold=8):
    risks = compute_risks(command, threshold)
    if review is not None:
        if not isinstance(review, dict) or set(review) != {"concurrency", "environment", "confirmation"}:
            raise ValueError("compute_review requires concurrency, environment and confirmation")
        if any(not isinstance(review[key], str) for key in ("environment", "confirmation")):
            raise ValueError("compute_review environment and confirmation must be text")
        concurrency = review["concurrency"]
        if isinstance(concurrency, bool) or not isinstance(concurrency, (str, int)) or isinstance(concurrency, int) and concurrency <= 0:
            raise ValueError("compute_review concurrency must be a positive integer or a description")
        if any(not str(value).strip() for value in review.values()) or any(len(str(value)) > 4096 for value in review.values()):
            raise ValueError("compute_review values must be nonempty and at most 4096 characters")
        # The trusted agent reports the user's chat confirmation. This is not
        # independent verification of the conversation or an OS security boundary.
        if audit.operation_id.get():
            audit.update(audit.operation_id.get(), compute_review=dict(review, evidence="agent_reported_user_confirmation"))
        return None
    if risks:
        return {"error": "compute_review_required", "executed": False, "command": command,
                "host": host, "cwd": cwd, "reasons": risks,
                "message": "Show command, concurrency, environment and host/cwd; wait for explicit user confirmation before resubmitting with compute_review."}
    return None
