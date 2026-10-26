"""A small, local request ledger for usage and explicitly supplied credit rates.

Normalized input is a JSON object with ``schema_version: 1`` and a ``requests``
array. Every request has ``agent``, ``source``, a globally stable ``request_id``, and ``scope``
(``task``, ``experiment``, or ``unallocated``). Token fields may be nonnegative
integers or null: ``input_tokens``, ``cached_input_tokens``, ``output_tokens``,
``cache_write_input_tokens``, ``reasoning_output_tokens``, and ``total_tokens``.
A null or absent field is unknown, never zero. Cached and cache-write input are
input details; reasoning output is a subset of output.
For example, a request is ``{"agent":"local","source":"run:local",
"request_id":"provider-or-synthetic-id","scope":"experiment",
"scope_id":"pair-1","model":"model-x","speed_mode":"fast",
"input_tokens":100,"cached_input_tokens":40,"output_tokens":20,
"reasoning_output_tokens":5,"total_tokens":120}``. Optional request context
includes ``reasoning_effort`` and ``service_tier``; ``reasoning_effort`` is not
a billing speed mode. Optional provenance labels are
``phase``, ``outcome``, ``failure_kind``, ``retry_of``, ``handoff_id``,
``handoff_from``, ``handoff_to``, and an arbitrary JSON ``tags`` object.

An optional rate snapshot must explicitly identify ``id``, ``source``,
``as_of``, ``model``, ``speed_mode``, ``unit`` (currently
``credits_per_million_tokens``), and a ``rates`` object with per-million
``input``, ``cached_input``, and ``output`` rates. Estimates are not produced
unless model and speed_mode match exactly. ``actual_credit_debit`` is credits;
``actual_currency_amount`` must travel with an explicit ``actual_currency``.
Actual debit and API-currency amounts are reported separately from estimates.
"""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
RATE_UNIT = "credits_per_million_tokens"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
CORE_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
PRICE_COMPONENTS = ("input", "cached_input", "output")
LABEL_FIELDS = (
    "phase",
    "outcome",
    "failure_kind",
    "retry_of",
    "handoff_id",
    "handoff_from",
    "handoff_to",
)


class LedgerError(ValueError):
    """Invalid normalized usage, rate, or worker-usage input."""


class DuplicateRequestConflict(LedgerError):
    """A stable request identity appeared with incompatible ledger values."""


def _nonempty_text(value: Any, name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{name} must be nonempty text" if not allow_none else f"{name} must be text or null")
    return value


def _token_count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise LedgerError(f"{name} must be a nonnegative integer or null (bool is not an integer here)")
    return value


def _decimal_amount(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise LedgerError(f"{name} must be an exact decimal string, integer, or Decimal")
    if not isinstance(value, (str, int, Decimal)):
        raise LedgerError(f"{name} must be an exact decimal string, integer, or Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise LedgerError(f"{name} must be a valid decimal amount") from exc
    if not result.is_finite() or result < 0:
        raise LedgerError(f"{name} must be finite and nonnegative")
    return result


def normalize_request(record: Any) -> dict[str, Any]:
    """Validate and copy one normalized request, preserving unknown usage as null."""
    if not isinstance(record, dict):
        raise LedgerError("each request must be an object")

    result: dict[str, Any] = {
        "agent": _nonempty_text(record.get("agent"), "agent"),
        "source": _nonempty_text(record.get("source"), "source"),
        "request_id": _nonempty_text(record.get("request_id"), "request_id"),
        "scope": record.get("scope"),
        "scope_id": _nonempty_text(record.get("scope_id"), "scope_id", allow_none=True),
        "model": _nonempty_text(record.get("model"), "model", allow_none=True),
        "speed_mode": _nonempty_text(record.get("speed_mode"), "speed_mode", allow_none=True),
        "speed_mode_source": _nonempty_text(record.get("speed_mode_source"), "speed_mode_source", allow_none=True),
        "reasoning_effort": _nonempty_text(record.get("reasoning_effort"), "reasoning_effort", allow_none=True),
        "service_tier": _nonempty_text(record.get("service_tier"), "service_tier", allow_none=True),
    }
    if result["scope"] not in ("task", "experiment", "unallocated"):
        raise LedgerError("scope must be 'task', 'experiment', or 'unallocated'")

    for field in TOKEN_FIELDS:
        result[field] = _token_count(record.get(field), field)
    if (result["input_tokens"] is not None and result["cached_input_tokens"] is not None
            and result["cached_input_tokens"] > result["input_tokens"]):
        raise LedgerError("cached_input_tokens cannot exceed input_tokens")
    if (result["input_tokens"] is not None and result["cache_write_input_tokens"] is not None
            and result["cache_write_input_tokens"] > result["input_tokens"]):
        raise LedgerError("cache_write_input_tokens cannot exceed input_tokens")
    if (result["output_tokens"] is not None and result["reasoning_output_tokens"] is not None
            and result["reasoning_output_tokens"] > result["output_tokens"]):
        raise LedgerError("reasoning_output_tokens cannot exceed output_tokens")
    if all(result[field] is not None for field in ("input_tokens", "output_tokens", "total_tokens")):
        if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
            raise LedgerError("total_tokens must equal input_tokens + output_tokens")

    for field in LABEL_FIELDS:
        result[field] = _nonempty_text(record.get(field), field, allow_none=True)
    tags = record.get("tags", {})
    if not isinstance(tags, dict):
        raise LedgerError("tags must be an object")
    result["tags"] = deepcopy(tags)
    result["synthetic_request_id"] = record.get("synthetic_request_id", False)
    if type(result["synthetic_request_id"]) is not bool:
        raise LedgerError("synthetic_request_id must be a boolean")

    result["actual_credit_debit"] = _decimal_amount(
        record.get("actual_credit_debit"), "actual_credit_debit"
    )
    result["actual_currency_amount"] = _decimal_amount(
        record.get("actual_currency_amount"), "actual_currency_amount"
    )
    result["actual_currency"] = _nonempty_text(
        record.get("actual_currency"), "actual_currency", allow_none=True
    )
    if (result["actual_currency_amount"] is None) != (result["actual_currency"] is None):
        raise LedgerError("actual_currency_amount and actual_currency must be supplied together")
    return result


def normalize_rate_snapshot(snapshot: Any) -> dict[str, Any] | None:
    """Validate an explicitly supplied one-model, one-speed-mode rate snapshot."""
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise LedgerError("rate_snapshot must be an object")
    result: dict[str, Any] = {
        key: _nonempty_text(snapshot.get(key), key)
        for key in ("id", "source", "as_of", "model", "speed_mode", "unit")
    }
    if result["unit"] != RATE_UNIT:
        raise LedgerError(f"rate_snapshot.unit must be {RATE_UNIT!r}")
    rates = snapshot.get("rates")
    if not isinstance(rates, dict):
        raise LedgerError("rate_snapshot.rates must be an object")
    result["rates"] = {
        component: _decimal_amount(rates.get(component), f"rate_snapshot.rates.{component}")
        for component in PRICE_COMPONENTS
    }
    return result


def import_worker_usage(
    payload: Any,
    *,
    agent: str = "worker",
    source: str = "worker-usage",
    scope: str = "unallocated",
    scope_id: str | None = None,
) -> list[dict[str, Any]]:
    """Import per-request ``request_events`` from a worker-usage JSON object.

    Request identity normally uses ``worker:<thread_id>:request:<sequence>``;
    where no valid sequence exists, an explicit provider_request_id or request_id
    is namespaced with the thread ID. ``contexts.effort``
    is retained as reasoning_effort and never used as speed_mode; an explicit
    ``contexts.speed_mode`` is required for rate matching. The cumulative
    ``total_token_usage`` values and top-level ``usage`` summary are deliberately
    ignored; only each event's single-request ``usage`` is recorded. An event
    must have a nonnegative integer request sequence or explicit provider_request_id
    / request_id; array position is never used as identity.
    """
    if not isinstance(payload, dict):
        raise LedgerError("worker usage input must be an object")
    thread_id = _nonempty_text(payload.get("thread_id"), "thread_id")
    agent = _nonempty_text(agent, "agent")
    source = _nonempty_text(source, "source")
    if scope not in ("task", "experiment", "unallocated"):
        raise LedgerError("scope must be 'task', 'experiment', or 'unallocated'")
    scope_id = _nonempty_text(scope_id, "scope_id", allow_none=True)

    contexts = payload.get("contexts")
    model = speed_mode = speed_mode_source = reasoning_effort = service_tier = None
    if isinstance(contexts, list) and contexts:
        normalized_contexts = []
        for context in contexts:
            if isinstance(context, dict):
                candidate_model = context.get("model")
                candidate_speed_mode = context.get("speed_mode")
                candidate_speed_mode_source = "contexts.speed_mode" if candidate_speed_mode is not None else None
                candidate_reasoning_effort = context.get("reasoning_effort", context.get("effort"))
                normalized_contexts.append((candidate_model, candidate_speed_mode, candidate_speed_mode_source,
                                            candidate_reasoning_effort, context.get("service_tier")))
        if normalized_contexts and all(item == normalized_contexts[0] for item in normalized_contexts):
            model, speed_mode, speed_mode_source, reasoning_effort, service_tier = normalized_contexts[0]
            if not isinstance(model, str) or not model.strip():
                model = None
            if not isinstance(speed_mode, str) or not speed_mode.strip():
                speed_mode = None
                speed_mode_source = None
            if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
                reasoning_effort = None
            if service_tier is not None and not isinstance(service_tier, str):
                service_tier = None

    events = payload.get("request_events", [])
    if not isinstance(events, list):
        raise LedgerError("request_events must be an array")
    requests = []
    for position, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise LedgerError(f"request_events[{position - 1}] must be an object")
        sequence = event.get("request")
        if type(sequence) is int and sequence >= 0:
            request_id = f"worker:{thread_id}:request:{sequence}"
        else:
            provider_id = event.get("provider_request_id", event.get("request_id"))
            if (isinstance(provider_id, str) and provider_id.strip()) or (
                type(provider_id) is int and provider_id >= 0
            ):
                request_id = f"worker:{thread_id}:provider:{provider_id}"
            else:
                raise LedgerError(
                    f"request_events[{position - 1}] needs a nonnegative integer request sequence "
                    "or a nonempty provider_request_id/request_id"
                )
        usage = event.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise LedgerError(f"request_events[{position - 1}].usage must be an object")
        request = {
            "agent": agent,
            "source": source,
            "request_id": request_id,
            "synthetic_request_id": True,
            "scope": scope,
            "scope_id": scope_id,
            "model": model,
            "speed_mode": speed_mode,
            "speed_mode_source": speed_mode_source,
            "reasoning_effort": reasoning_effort,
            "service_tier": service_tier,
            **{field: usage.get(field) for field in TOKEN_FIELDS},
            **{field: event.get(field) for field in LABEL_FIELDS},
            "tags": event.get("tags", {}),
        }
        requests.append(normalize_request(request))
    return requests


def _identity(request: dict[str, Any]) -> str:
    return request["request_id"]


def _identity_text(identity: str) -> str:
    return identity


def _exact_sum(values: list[Decimal]) -> Decimal:
    """Add finite decimals without context rounding."""
    if not values:
        return Decimal(0)
    parts = [value.as_tuple() for value in values]
    exponent = min(part.exponent for part in parts)
    coefficient = 0
    for part in parts:
        digits = int("".join(str(digit) for digit in part.digits) or "0")
        signed = -digits if part.sign else digits
        coefficient += signed * (10 ** (part.exponent - exponent))
    sign = 1 if coefficient < 0 else 0
    digits_text = str(abs(coefficient))
    return Decimal((sign, tuple(int(char) for char in digits_text), exponent))


def _scaled_rate(token_count: int, rate: Decimal) -> Decimal:
    """Return rate * tokens / 1,000,000 as an exact Decimal tuple."""
    part = rate.as_tuple()
    coefficient = int("".join(str(digit) for digit in part.digits) or "0") * token_count
    digits_text = str(coefficient)
    return Decimal((part.sign, tuple(int(char) for char in digits_text), part.exponent - 6))


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _percent(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return _decimal_text(Decimal(numerator) * Decimal(100) / Decimal(denominator))


def _billable_usage_complete(request: dict[str, Any]) -> bool:
    return all(request[field] is not None for field in CORE_USAGE_FIELDS)


def _request_estimate(request: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        return {
            "status": "unknown",
            "rate_snapshot_id": None,
            "estimated_credits": None,
            "estimated_credits_complete": False,
            "priced_components": [],
            "missing_components": ["rate_snapshot"],
            "reason": "no_explicit_rate_snapshot",
        }

    mismatch = []
    if request["model"] is None:
        mismatch.append("model_missing")
    elif request["model"] != snapshot["model"]:
        mismatch.append("model_mismatch")
    if request["speed_mode"] is None:
        mismatch.append("speed_mode_missing")
    elif request["speed_mode"] != snapshot["speed_mode"]:
        mismatch.append("speed_mode_mismatch")
    if mismatch:
        return {
            "status": "unknown",
            "rate_snapshot_id": snapshot["id"],
            "estimated_credits": None,
            "estimated_credits_complete": False,
            "priced_components": [],
            "missing_components": list(PRICE_COMPONENTS),
            "reason": mismatch,
        }

    usage_by_component: dict[str, int | None] = {
        "input": None if request["input_tokens"] is None or request["cached_input_tokens"] is None
        else request["input_tokens"] - request["cached_input_tokens"],
        "cached_input": request["cached_input_tokens"],
        "output": request["output_tokens"],
    }
    priced_components = []
    missing_components = []
    costs: list[Decimal] = []
    for component in PRICE_COMPONENTS:
        token_count = usage_by_component[component]
        rate = snapshot["rates"][component]
        if token_count is None or rate is None:
            reasons = []
            if token_count is None:
                reasons.append("usage_missing")
            if rate is None:
                reasons.append("rate_missing")
            missing_components.append({"component": component, "reasons": reasons})
            continue
        cost = _scaled_rate(token_count, rate)
        costs.append(cost)
        priced_components.append({
            "component": component,
            "tokens": token_count,
            "rate_per_million": _decimal_text(rate),
            "credits": _decimal_text(cost),
        })

    if len(priced_components) == len(PRICE_COMPONENTS) and _billable_usage_complete(request):
        status = "complete"
    elif priced_components:
        status = "partial"
    else:
        status = "unknown"
    estimated = _exact_sum(costs) if costs else None
    return {
        "status": status,
        "rate_snapshot_id": snapshot["id"],
        "estimated_credits": _decimal_text(estimated),
        "estimated_credits_complete": status == "complete",
        "priced_components": priced_components,
        "missing_components": missing_components,
        "reason": None if status == "complete" else "incomplete_usage_or_rates",
    }


def _sum_token_field(requests: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [request[field] for request in requests if request[field] is not None]
    missing = len(requests) - len(values)
    return {
        "known_sum": sum(values) if values else None,
        "known_requests": len(values),
        "missing_requests": missing,
        "complete": missing == 0,
    }


def _sum_actual_credits(requests: list[dict[str, Any]]) -> dict[str, Any]:
    values = [request["actual_credit_debit"] for request in requests
              if request["actual_credit_debit"] is not None]
    missing = len(requests) - len(values)
    return {
        "known_sum": _decimal_text(_exact_sum(values)) if values else None,
        "known_requests": len(values),
        "missing_requests": missing,
        "complete": missing == 0,
    }


def _sum_currency_amounts(requests: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[Decimal]] = {}
    counts: dict[str, int] = {}
    unknown = 0
    for request in requests:
        amount, currency = request["actual_currency_amount"], request["actual_currency"]
        if amount is None or currency is None:
            unknown += 1
            continue
        groups.setdefault(currency, []).append(amount)
        counts[currency] = counts.get(currency, 0) + 1
    return {
        "by_currency": {
            currency: {
                "known_sum": _decimal_text(_exact_sum(amounts)),
                "known_requests": counts[currency],
            }
            for currency, amounts in sorted(groups.items())
        },
        "unknown_requests": unknown,
    }


def _status_for_estimates(estimates: list[dict[str, Any]]) -> str:
    if estimates and all(item["status"] == "complete" for item in estimates):
        return "complete"
    if any(item["priced_components"] for item in estimates):
        return "partial"
    return "unknown"


def _aggregate(requests: list[dict[str, Any]], estimates: list[dict[str, Any]], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    token_sums = {field: _sum_token_field(requests, field) for field in TOKEN_FIELDS}
    known_input_and_cache = [
        request["input_tokens"] - request["cached_input_tokens"]
        for request in requests
        if request["input_tokens"] is not None and request["cached_input_tokens"] is not None
    ]
    missing_input_and_cache = len(requests) - len(known_input_and_cache)
    component_costs = [
        Decimal(component["credits"])
        for estimate in estimates
        for component in estimate["priced_components"]
    ]
    all_costed = len(requests) > 0 and all(item["status"] == "complete" for item in estimates)
    estimate_status = _status_for_estimates(estimates)
    billable_usage_complete_count = sum(_billable_usage_complete(request) for request in requests)
    reasoning_breakdown_count = sum(request["reasoning_output_tokens"] is not None for request in requests)
    actual = _sum_actual_credits(requests)
    return {
        "request_count": len(requests),
        "usage_status": "complete" if billable_usage_complete_count == len(requests) and requests else "partial",
        "complete_usage_requests": billable_usage_complete_count,
        "partial_usage_requests": len(requests) - billable_usage_complete_count,
        "reasoning_breakdown_status": (
            "complete" if reasoning_breakdown_count == len(requests) and requests
            else "partial" if reasoning_breakdown_count else "unknown"
        ),
        "reasoning_breakdown_requests": reasoning_breakdown_count,
        "missing_reasoning_breakdown_requests": len(requests) - reasoning_breakdown_count,
        "token_totals": token_sums,
        "non_cached_input_tokens": {
            "known_sum": sum(known_input_and_cache) if known_input_and_cache else None,
            "known_requests": len(known_input_and_cache),
            "missing_requests": missing_input_and_cache,
            "complete": missing_input_and_cache == 0,
        },
        "estimation": {
            "status": estimate_status,
            "rate_snapshot_id": snapshot["id"] if snapshot else None,
            "fully_estimated_requests": sum(item["status"] == "complete" for item in estimates),
            "partially_estimated_requests": sum(item["status"] == "partial" for item in estimates),
            "unknown_estimate_requests": sum(item["status"] == "unknown" for item in estimates),
            "priced_component_count": sum(len(item["priced_components"]) for item in estimates),
            "total_component_count": 3 * len(requests),
            "component_coverage_percent": _percent(
                sum(len(item["priced_components"]) for item in estimates), 3 * len(requests)
            ),
            "known_component_credits": _decimal_text(_exact_sum(component_costs)) if component_costs else None,
            "estimated_credits": _decimal_text(_exact_sum(component_costs)) if all_costed else None,
        },
        "actual_credit_debit": actual,
        "actual_credit_debit_status": (
            "complete" if actual["complete"] and requests
            else "partial" if actual["known_requests"] else "unknown"
        ),
        "actual_currency_costs": _sum_currency_amounts(requests),
    }


def build_cost_ledger(
    records: list[Any], rate_snapshot: Any = None, *, deduplicate: bool = True
) -> dict[str, Any]:
    """Build a deduplicated ledger and its request, scope, agent, and usage report.

    The stable deduplication key is ``request_id``. Identical
    duplicates count once. Any difference in a duplicate's normalized fields,
    including scope or usage, raises :class:`DuplicateRequestConflict`.
    """
    if not isinstance(records, list):
        raise LedgerError("requests must be an array")
    snapshot = normalize_rate_snapshot(rate_snapshot)
    requests: list[dict[str, Any]] = []
    identities: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for raw in records:
        request = normalize_request(raw)
        identity = _identity(request)
        previous = identities.get(identity)
        if previous is not None:
            if not deduplicate:
                raise DuplicateRequestConflict(f"duplicate request identity: {_identity_text(identity)}")
            if previous != request:
                raise DuplicateRequestConflict(f"conflicting duplicate request identity: {_identity_text(identity)}")
            duplicate_count += 1
            continue
        identities[identity] = request
        requests.append(request)

    estimates = [_request_estimate(request, snapshot) for request in requests]
    report = _aggregate(requests, estimates, snapshot)
    by_scope: dict[tuple[str, str | None], list[int]] = {}
    by_agent: dict[str, list[int]] = {}
    for index, request in enumerate(requests):
        by_scope.setdefault((request["scope"], request["scope_id"]), []).append(index)
        by_agent.setdefault(request["agent"], []).append(index)

    report["deduplication"] = {
        "key": ["request_id"],
        "duplicate_requests_dropped": duplicate_count,
        "conflicting_duplicates": "rejected",
    }
    report["field_relationships"] = {
        "cached_input_tokens": "subset_of_input_tokens",
        "reasoning_output_tokens": "subset_of_output_tokens; not priced a second time",
        "cache_write_input_tokens": "input detail; retained separately and not added a second time to input pricing",
        "total_tokens": "input_tokens + output_tokens",
        "unallocated_scope": "explicitly captures costs that cannot be assigned to a task or experiment",
    }
    report["by_scope"] = []
    for (scope, scope_id), indices in sorted(by_scope.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        group_requests = [requests[index] for index in indices]
        group_estimates = [estimates[index] for index in indices]
        report["by_scope"].append({
            "scope": scope,
            "scope_id": scope_id,
            **_aggregate(group_requests, group_estimates, snapshot),
        })
    unallocated_indices = [index for index, request in enumerate(requests)
                           if request["scope"] == "unallocated"]
    unallocated_requests = [requests[index] for index in unallocated_indices]
    unallocated_estimates = [estimates[index] for index in unallocated_indices]
    unallocated_summary = (
        _aggregate(unallocated_requests, unallocated_estimates, snapshot)
        if unallocated_requests else None
    )
    unallocated_scope_ids = sorted(
        {request["scope_id"] for request in unallocated_requests}, key=lambda value: value or ""
    )
    report["unallocated_costs"] = {
        "status": "recorded" if unallocated_summary is not None else "no_unallocated_requests_in_input",
        "recorded_requests": unallocated_summary["request_count"] if unallocated_summary else None,
        "scope_ids": unallocated_scope_ids,
        "costs_not_inferred": True,
        "note": "Enter unassigned controller or orchestration requests with scope='unallocated'; absence of records is not proof of zero cost.",
        "aggregate": unallocated_summary,
    }
    report["by_agent"] = []
    for agent, indices in sorted(by_agent.items()):
        group_requests = [requests[index] for index in indices]
        group_estimates = [estimates[index] for index in indices]
        report["by_agent"].append({
            "agent": agent,
            **_aggregate(group_requests, group_estimates, snapshot),
        })

    report["audit_status"] = (
        "complete" if report["usage_status"] == "complete"
        and report["estimation"]["status"] == "complete" else "partial"
    )
    report["coverage_scope"] = {
        "ledger_entries": "only the supplied, deduplicated request records",
        "all_task_participants_or_requests_verified": False,
        "actual_credit_debit": "only explicitly supplied per-request values; not independently verified",
        "audit_status_means": "usage and estimates are complete for supplied entries only; it does not establish full task or account cost coverage",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "rate_snapshot": snapshot,
        "requests": [dict(request, estimate=estimate) for request, estimate in zip(requests, estimates)],
        "report": report,
    }


def load_json(path: str | Path) -> Any:
    """Read JSON while keeping decimal literals exact."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"could not read JSON input {Path(path).name}: {exc}") from exc
