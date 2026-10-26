#!/usr/bin/env python3
"""Build a local request-usage ledger and optional explicit credit estimate.

Input may be a normalized ledger JSON file (schema_version 1 with a requests
array) or worker-usage JSON with thread_id, contexts, and request_events. For
normalized records, each request requires agent, source, request_id, and scope;
token fields input_tokens, cached_input_tokens, cache_write_input_tokens,
output_tokens, reasoning_output_tokens, and total_tokens are nonnegative
integers or null. Omitted values stay unknown. Worker contexts.effort is
reasoning_effort, never speed_mode; only an explicit speed_mode can match rates.
Each worker event must carry a nonnegative integer request sequence or an
explicit provider_request_id/request_id; array position is never used as identity.

Rate snapshots are supplied with --rate-snapshot and must contain id, source,
as_of, model, speed_mode, unit=credits_per_million_tokens, and rates for input,
cached_input, and output. Each amount is an exact decimal string (for example
"0.25"). Model and speed_mode must match exactly; there is no Standard fallback.

Exit codes: 0 means usage and estimates are complete, 1 means a valid but
partial/unknown audit, and 2 means invalid input or an I/O error. Actual credit
debits and API-currency amounts remain separate from estimated credits.
The report's audit_status applies only to the supplied, deduplicated entries; it
does not verify every task participant, request, controller, or real debit.
"""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any

from remote_workbench.cost_ledger import (
    LedgerError,
    SCHEMA_VERSION,
    build_cost_ledger,
    import_worker_usage,
    load_json,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _parallel_values(values: list[str] | None, paths: list[Path], default) -> list[str]:
    if not values:
        return [default(path) for path in paths]
    if len(values) == 1:
        return values * len(paths)
    if len(values) == len(paths):
        return values
    raise LedgerError("repeat --agent/--source once, or once per input file")


def _is_normalized(payload: Any) -> bool:
    return isinstance(payload, dict) and "requests" in payload


def build_from_inputs(args: argparse.Namespace) -> dict[str, Any]:
    paths = args.inputs
    agents = _parallel_values(args.agent, paths, lambda _path: "worker")
    sources = _parallel_values(args.source, paths, lambda path: path.name)
    all_requests = []
    embedded_snapshots = []

    for index, path in enumerate(paths):
        payload = load_json(path)
        input_format = args.format
        if input_format == "auto":
            input_format = "normalized" if _is_normalized(payload) else "worker-usage"
        if input_format == "normalized":
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                raise LedgerError(f"{path.name}: normalized input must have schema_version {SCHEMA_VERSION}")
            requests = payload.get("requests")
            if not isinstance(requests, list):
                raise LedgerError(f"{path.name}: requests must be an array")
            all_requests.extend(requests)
            if payload.get("rate_snapshot") is not None:
                embedded_snapshots.append(payload["rate_snapshot"])
        else:
            all_requests.extend(import_worker_usage(
                payload,
                agent=agents[index],
                source=sources[index],
                scope=args.scope,
                scope_id=args.scope_id,
            ))

    explicit_snapshot = load_json(args.rate_snapshot) if args.rate_snapshot else None
    if explicit_snapshot is not None:
        selected_snapshot = explicit_snapshot
    elif embedded_snapshots:
        selected_snapshot = embedded_snapshots[0]
        for other in embedded_snapshots[1:]:
            if other != selected_snapshot:
                raise LedgerError("normalized inputs contain different rate snapshots; supply one explicit --rate-snapshot")
    else:
        selected_snapshot = None
    return build_cost_ledger(all_requests, selected_snapshot)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="normalized ledger or worker-usage JSON files")
    parser.add_argument("--format", choices=("auto", "normalized", "worker-usage"), default="auto")
    parser.add_argument("--agent", action="append", help="agent label; repeat once per input, or once for all")
    parser.add_argument("--source", action="append", help="provenance label; defaults to each input filename")
    parser.add_argument("--scope", choices=("task", "experiment", "unallocated"), default="unallocated",
                        help="scope assigned to imported worker events")
    parser.add_argument("--scope-id", help="optional task/experiment identifier for imported worker events")
    parser.add_argument("--rate-snapshot", type=Path, help="explicit user-supplied rate snapshot JSON")
    parser.add_argument("--output", type=Path, help="write the machine-readable result to this JSON file")
    args = parser.parse_args(argv)
    try:
        result = build_from_inputs(args)
        rendered = json.dumps(_json_value(result), ensure_ascii=False, separators=(",", ":"))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return 0 if result["report"]["audit_status"] == "complete" else 1
    except (OSError, LedgerError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "invalid_input", "error": str(exc)}, ensure_ascii=False,
                         separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
