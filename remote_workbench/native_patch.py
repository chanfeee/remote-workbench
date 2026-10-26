"""Compile context patches to checked Git patches without model-written counts.

This stdlib-only source is also sent to the remote Python worker. It never
executes a command or writes a file; the worker validates/applies the resulting
multi-file patch once with Git. Matching is exact and ambiguous hunks fail.
"""
import difflib
import json
from pathlib import Path
import stat


def compile_native_patch(text, repo, max_file_size=10485760):
    lines = text.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Expected *** Begin Patch and *** End Patch")
    repo = Path(repo).resolve()
    used = set()
    patches = []
    position = 1

    def path_for(name):
        if not name or any(ord(c) < 32 for c in name):
            raise ValueError("Invalid patch path")
        path = Path(name)
        if not path.is_absolute():
            path = repo / path
        try:
            relative = path.relative_to(repo)
        except ValueError:
            raise ValueError("Patch path is outside the repository: " + name)
        if not relative.parts or any(p == ".." or p.casefold() == ".git" for p in relative.parts):
            raise ValueError("Unsafe patch path: " + name)
        # A symlink in any path component could otherwise escape the worktree.
        if path.resolve() != path or path.is_symlink():
            raise ValueError("Refusing a symbolic-link patch path: " + name)
        if path in used:
            raise ValueError("Patch path occurs more than once: " + name)
        used.add(path)
        return path, relative.as_posix()

    def read(path):
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_file_size:
            raise ValueError("Patch input must be a regular file within the size limit")
        data = path.read_bytes()
        if len(data) > max_file_size:
            raise ValueError("Patch input exceeds the size limit")
        return data.decode("utf-8"), bool(info.st_mode & 0o111)

    def emit(name, before, after, executable=False):
        if before is not None and after is not None and before == after:
            return
        a, b = json.dumps("a/" + name, ensure_ascii=False), json.dumps("b/" + name, ensure_ascii=False)
        header = "diff --git " + a + " " + b + "\n"
        mode = "100755" if executable else "100644"
        if before is None:
            header += "new file mode " + mode + "\n"
        if after is None:
            header += "deleted file mode " + mode + "\n"
        body = difflib.unified_diff(
            (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
            fromfile=a if before is not None else "/dev/null",
            tofile=b if after is not None else "/dev/null",
        )
        patches.append(header + "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in body
        ))

    while position < len(lines) - 1:
        directive = lines[position]
        position += 1
        kind = next((k for k in ("Add", "Update", "Delete")
                     if directive.startswith("*** " + k + " File: ")), None)
        if kind is None:
            raise ValueError("Expected Add, Update or Delete File, got: " + directive[:120])
        path, name = path_for(directive[len("*** " + kind + " File: "):])
        if kind == "Add":
            if path.exists():
                raise ValueError("Add destination already exists: " + name)
            added = []
            while position < len(lines) - 1 and not lines[position].startswith("*** "):
                line = lines[position]
                if not line.startswith("+"):
                    raise ValueError("Added file lines must start with +")
                added.append(line[1:])
                position += 1
            content = "\n".join(added) + ("\n" if added else "")
            if len(content.encode()) > max_file_size:
                raise ValueError("Patch result exceeds the size limit")
            emit(name, None, content)
            continue
        before, executable = read(path)
        if kind == "Delete":
            emit(name, before, None, executable)
            continue
        destination = None
        if position < len(lines) - 1 and lines[position].startswith("*** Move to: "):
            destination, destination_name = path_for(lines[position][len("*** Move to: "):])
            if destination.exists():
                raise ValueError("Move destination already exists: " + destination_name)
            position += 1
        newline = "\r\n" if "\r\n" in before else "\n"
        if newline == "\r\n" and "\n" in before.replace("\r\n", ""):
            raise ValueError("Mixed newline styles need a unified Git patch: " + name)
        original = before.splitlines()
        result, cursor, hunks = [], 0, 0
        while position < len(lines) - 1 and not lines[position].startswith("*** "):
            if not (lines[position] == "@@" or lines[position].startswith("@@ ")):
                raise ValueError("Expected @@ before update hunk")
            anchor = lines[position][3:] if lines[position].startswith("@@ ") else ""
            search_start = cursor
            if anchor:
                anchors = [i for i in range(cursor, len(original)) if original[i] == anchor]
                if len(anchors) != 1:
                    raise ValueError("Hunk anchor is missing or ambiguous: " + name)
                search_start = anchors[0] + 1
            position += 1
            old, new = [], []
            while position < len(lines) - 1:
                line = lines[position]
                if line.startswith("*** ") or line == "@@" or line.startswith("@@ "):
                    break
                if not line:
                    line = " "  # Native context patches permit an empty context line.
                if line[0] not in " +-":
                    raise ValueError("Hunk lines must start with space, + or -")
                if line[0] in " -":
                    old.append(line[1:])
                if line[0] in " +":
                    new.append(line[1:])
                position += 1
            at_end = position < len(lines) - 1 and lines[position] == "*** End of File"
            if at_end:
                position += 1
            if not old and not at_end:
                raise ValueError("An insertion needs exact context or *** End of File")
            matches = [i for i in range(search_start, len(original) - len(old) + 1)
                       if original[i:i + len(old)] == old
                       and (not at_end or i + len(old) == len(original))]
            if len(matches) != 1:
                raise ValueError("Hunk context is " + ("missing" if not matches else "ambiguous") + ": " + name)
            start = matches[0]
            result.extend(original[cursor:start])
            result.extend(new)
            cursor = start + len(old)
            hunks += 1
        if not hunks:
            raise ValueError("Update needs at least one hunk: " + name)
        result.extend(original[cursor:])
        after = newline.join(result) + (newline if result and before.endswith("\n") else "")
        if len(after.encode()) > max_file_size:
            raise ValueError("Patch result exceeds the size limit")
        if destination is not None:
            emit(name, before, None, executable)
            emit(destination_name, None, after, executable)
        else:
            emit(name, before, after, executable)
    if not used:
        raise ValueError("Patch needs at least one file operation")
    return "".join(patches)
