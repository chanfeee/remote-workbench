"""Bounded literal search over output already saved in the local audit store."""
from __future__ import annotations

import json
from pathlib import Path

from remote_workbench import audit


def search_saved_output(
    output_id: str,
    query: str,
    stream: str = "stdout",
    cursor: int = 0,
    max_scan_bytes: int = 65536,
    context_bytes: int = 96,
    max_matches: int = 20,
    max_bytes: int = 8192,
) -> dict:
    """Search one local spool page and return byte offsets with small contexts.

    ``cursor`` and ``next_cursor`` are byte offsets, matching ``read_output``.
    A cursor only advances past examined bytes; if the result count or response
    budget is reached, the first omitted match remains available on the next
    call. Searching reads no remote state and never opens an SSH connection.
    """
    if stream not in audit.STREAMS:
        raise ValueError("Unknown stream")
    if not isinstance(query, str) or not query:
        raise ValueError("query must be nonempty text")
    needle = query.encode("utf-8")
    if len(needle) > 256:
        raise ValueError("query must be at most 256 UTF-8 bytes")
    values = {
        "cursor": (cursor, 0, 2**63 - 1),
        "max_scan_bytes": (max_scan_bytes, 1, 1048576),
        "context_bytes": (context_bytes, 0, 512),
        "max_matches": (max_matches, 1, 100),
        "max_bytes": (max_bytes, 1024, 32768),
    }
    for name, (value, lower, upper) in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer between {lower} and {upper}")

    path: Path = audit.directory(output_id) / stream
    with path.open("rb") as source:
        total_before = path.stat().st_size
        if cursor > total_before:
            raise ValueError("cursor exceeds available bytes")
        scan_end = min(total_before, cursor + max_scan_bytes)
        source.seek(cursor)
        # Read a small overlap so a literal crossing the page boundary is not
        # lost. Only matches beginning before scan_end belong to this page.
        data = source.read((scan_end - cursor) + len(needle) - 1)

        found: list[int] = []
        position = 0
        while len(found) <= max_matches:
            position = data.find(needle, position)
            if position < 0:
                break
            absolute = cursor + position
            if absolute >= scan_end:
                break
            found.append(absolute)
            position += 1  # Preserve overlapping literal matches.

        def make_match(offset: int) -> dict:
            left = max(0, offset - context_bytes)
            right = min(total_before, offset + len(needle) + context_bytes)
            source.seek(left)
            context = source.read(right - left)
            return {
                "offset": offset,
                "context_start": left,
                "context": context.decode("utf-8", errors="replace"),
                "context_truncated_before": left > 0,
                "context_truncated_after": right < total_before,
            }

        response = {
            "output_id": output_id,
            "stream": stream,
            "matches": [],
            "next_cursor": scan_end,
            "total_bytes": total_before,
            "scanned_bytes": scan_end - cursor,
            "truncated": scan_end < total_before,
        }
        # Probe one beyond the visible count so a saturated page can signal
        # truncation and resume exactly at the first omitted match.
        visible = found[:max_matches]
        if len(found) > max_matches:
            response["next_cursor"] = found[max_matches]
            response["truncated"] = True

        total_after = path.stat().st_size
        response["total_bytes"] = total_after
        if response["next_cursor"] < total_after:
            response["truncated"] = True

        for offset in visible:
            response["matches"].append(make_match(offset))
            if len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_bytes:
                response["matches"].pop()
                response["next_cursor"] = offset
                response["truncated"] = True
                break

        # Final metadata can change the encoded size by a few bytes, so clamp
        # once more after all cursors and truncation flags are settled.
        def response_size():
            return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        while response_size() > max_bytes and response["matches"]:
            omitted = response["matches"].pop()
            response["next_cursor"] = min(response["next_cursor"], omitted["offset"])
            response["truncated"] = True

        # Even a deliberately small response budget can always return the
        # next location without context, keeping pagination usable. Advance
        # past this returned match start so the next page cannot return it
        # forever; advancing by one byte still preserves overlapping matches.
        if not response["matches"] and visible and response["next_cursor"] == visible[0]:
            response["next_cursor"] = visible[0] + 1
            response["truncated"] = True
            response["matches"].append({
                "offset": visible[0],
                "context_start": visible[0],
                "context": "",
                "context_truncated_before": True,
                "context_truncated_after": True,
            })

        response["scanned_bytes"] = max(0, response["next_cursor"] - cursor)
        if response_size() > max_bytes:
            raise ValueError("max_bytes is too small for a match location")
        return response
