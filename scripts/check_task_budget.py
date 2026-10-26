"""Check one full-task usage pair; no tokenizer estimates or billing claims.

Inputs are normalized worker records with model, reasoning_effort, requests,
acceptance_passed, complete, and usage (input_tokens, cached_input_tokens,
output_tokens, total_tokens). Include every request, including error recovery.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re


def validate(record):
    if not isinstance(record, dict):
        raise ValueError("worker record must be an object")
    for key in ("model", "reasoning_effort"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise ValueError(key + " must be nonempty text")
    for key in ("baseline_sha256", "task_sha256"):
        if not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key]):
            raise ValueError(key + " must identify the frozen baseline/task with a SHA256")
    count = record.get("requests")
    if type(count) is not int or count < 1:
        raise ValueError("requests must be a positive integer")
    usage = record["usage"]
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise ValueError(key + " must be a nonnegative integer")
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise ValueError("cached input is a subset of input")
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"] or not usage["total_tokens"]:
        raise ValueError("total_tokens must equal input + output and be positive")


def compare(native, remote, threshold=Decimal("2"), expected_requests=9):
    validate(native)
    validate(remote)
    threshold = Decimal(str(threshold))
    if not threshold.is_finite() or threshold < 0:
        raise ValueError("threshold must be finite and nonnegative")
    baseline = native["usage"]["total_tokens"]
    actual = remote["usage"]["total_tokens"]
    percent = Decimal(actual - baseline) * 100 / baseline
    checks = {
        "same_baseline": native["baseline_sha256"] == remote["baseline_sha256"],
        "same_task": native["task_sha256"] == remote["task_sha256"],
        "same_model": native["model"] == remote["model"],
        "same_reasoning_effort": native["reasoning_effort"] == remote["reasoning_effort"],
        "expected_requests": native["requests"] == remote["requests"] == expected_requests,
        "both_accepted": native.get("acceptance_passed") is True and remote.get("acceptance_passed") is True,
        "both_complete": native.get("complete") is True and remote.get("complete") is True,
        "below_budget": percent <= threshold,
    }
    return {
        "result": "sample_within_budget" if all(checks.values()) else "not_qualified",
        "checks": checks, "native_total_tokens": baseline, "remote_total_tokens": actual,
        "additional_tokens": actual - baseline, "additional_percent_of_native": str(percent),
        "threshold_percent_max": str(threshold), "threshold_comparison": "<=",
        "expected_requests_each": expected_requests,
        "scope": "complete_worker_tasks_including_all_retries",
        "account_allowance_delta": None,
        "limitation": "One accepted pair is a sample, not a guarantee for other tasks. Acceptance and completeness require external evidence.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--remote", type=Path, required=True)
    parser.add_argument("--threshold", default="2", help="Maximum extra percentage of the native task (inclusive)")
    parser.add_argument("--requests", type=int, default=9)
    args = parser.parse_args()
    try:
        if args.requests < 1:
            raise ValueError("requests must be positive")
        report = compare(json.loads(args.native.read_text()), json.loads(args.remote.read_text()),
                         args.threshold, args.requests)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        print(json.dumps({"result": "invalid_input", "error": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["result"] == "sample_within_budget" else 1


if __name__ == "__main__":
    raise SystemExit(main())
