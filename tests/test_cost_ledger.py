from copy import deepcopy
from decimal import Decimal
import json

import pytest

from remote_workbench.cost_ledger import (
    DuplicateRequestConflict,
    LedgerError,
    build_cost_ledger,
    import_worker_usage,
)


def request(request_id="r1", **changes):
    value = {
        "agent": "agent-a",
        "source": "fixture",
        "request_id": request_id,
        "scope": "task",
        "scope_id": "task-1",
        "model": "model-x",
        "speed_mode": "fast",
        "reasoning_effort": "max",
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": 120,
    }
    value.update(changes)
    return value


def rates(**changes):
    value = {
        "id": "snapshot-2026-10-25",
        "source": "user-supplied-fixture",
        "as_of": "2026-10-25",
        "model": "model-x",
        "speed_mode": "fast",
        "unit": "credits_per_million_tokens",
        "rates": {"input": "0.3", "cached_input": "0.03", "output": "1"},
    }
    value.update(changes)
    return value


def test_identical_stable_identity_is_deduplicated_but_conflicts_reject():
    first = request()
    ledger = build_cost_ledger([first, deepcopy(first)])
    assert ledger["report"]["request_count"] == 1
    assert ledger["report"]["deduplication"]["duplicate_requests_dropped"] == 1

    conflict = request(input_tokens=101, total_tokens=121)
    with pytest.raises(DuplicateRequestConflict, match="conflicting duplicate"):
        build_cost_ledger([first, conflict])


def test_cached_input_is_a_subset_and_reasoning_is_not_added_again():
    record = request(input_tokens=100, cached_input_tokens=50, output_tokens=20,
                     reasoning_output_tokens=10, total_tokens=120)
    ledger = build_cost_ledger([record], rates())
    estimate = ledger["requests"][0]["estimate"]
    assert [part["component"] for part in estimate["priced_components"]] == ["input", "cached_input", "output"]
    assert [part["tokens"] for part in estimate["priced_components"]] == [50, 50, 20]
    assert estimate["estimated_credits"] == "0.0000365"
    assert ledger["report"]["token_totals"]["input_tokens"]["known_sum"] == 100
    assert ledger["report"]["token_totals"]["cached_input_tokens"]["known_sum"] == 50


def test_missing_rate_and_unknown_or_mismatched_speed_do_not_default():
    no_rate = build_cost_ledger([request()])
    assert no_rate["requests"][0]["estimate"]["status"] == "unknown"
    assert no_rate["requests"][0]["estimate"]["reason"] == "no_explicit_rate_snapshot"
    assert no_rate["report"]["estimation"]["component_coverage_percent"] == "0"
    assert no_rate["report"]["estimation"]["estimated_credits"] is None

    no_speed = build_cost_ledger([request(speed_mode=None)], rates())
    assert no_speed["requests"][0]["estimate"]["status"] == "unknown"
    assert "speed_mode_missing" in no_speed["requests"][0]["estimate"]["reason"]

    wrong_speed = build_cost_ledger([request()], rates(speed_mode="standard"))
    assert wrong_speed["requests"][0]["estimate"]["status"] == "unknown"
    assert "speed_mode_mismatch" in wrong_speed["requests"][0]["estimate"]["reason"]
    assert wrong_speed["report"]["estimation"]["fully_estimated_requests"] == 0


def test_rate_snapshot_is_explicit_and_uses_exact_decimal_arithmetic():
    ledger = build_cost_ledger([request()], rates())
    expected = Decimal(60) * Decimal("0.3") / Decimal(1_000_000)
    expected += Decimal(40) * Decimal("0.03") / Decimal(1_000_000)
    expected += Decimal(20) * Decimal("1") / Decimal(1_000_000)
    assert ledger["report"]["estimation"]["estimated_credits"] == format(expected, "f").rstrip("0").rstrip(".")
    assert ledger["report"]["estimation"]["status"] == "complete"
    assert ledger["rate_snapshot"]["id"] == "snapshot-2026-10-25"


def test_actual_credit_and_currency_cost_are_separate_from_estimates():
    record = request(actual_credit_debit="0.0005", actual_currency_amount="0.14", actual_currency="USD")
    ledger = build_cost_ledger([record], rates())
    report = ledger["report"]
    assert report["actual_credit_debit"]["known_sum"] == "0.0005"
    assert report["estimation"]["estimated_credits"] == "0.0000392"
    assert report["actual_currency_costs"]["by_currency"]["USD"]["known_sum"] == "0.14"
    assert "combined" not in report


def test_scopes_and_agents_keep_task_experiment_and_controller_costs_visible():
    records = [
        request("a1", agent="local", input_tokens=100, cached_input_tokens=10, output_tokens=10,
                reasoning_output_tokens=2, total_tokens=110),
        request("a2", agent="remote", input_tokens=20, cached_input_tokens=5, output_tokens=4,
                reasoning_output_tokens=1, total_tokens=24),
        request("a3", agent="local", scope="experiment", scope_id="experiment-1", input_tokens=30,
                cached_input_tokens=15, output_tokens=5, reasoning_output_tokens=None, total_tokens=35),
        request("a4", agent="controller", scope="unallocated", scope_id=None, input_tokens=4,
                cached_input_tokens=0, output_tokens=1, reasoning_output_tokens=None, total_tokens=5),
    ]
    report = build_cost_ledger(records, rates())["report"]
    assert report["request_count"] == 4
    assert report["token_totals"]["total_tokens"]["known_sum"] == 174
    assert {row["agent"]: row["request_count"] for row in report["by_agent"]} == {
        "controller": 1, "local": 2, "remote": 1,
    }
    by_scope = {(row["scope"], row["scope_id"]): row for row in report["by_scope"]}
    assert by_scope[("task", "task-1")]["request_count"] == 2
    assert by_scope[("task", "task-1")]["token_totals"]["total_tokens"]["known_sum"] == 134
    assert by_scope[("experiment", "experiment-1")]["request_count"] == 1
    assert by_scope[("unallocated", None)]["request_count"] == 1
    assert report["unallocated_costs"]["status"] == "recorded"
    assert report["unallocated_costs"]["recorded_requests"] == 1


def test_unallocated_summary_aggregates_all_scope_ids_without_hiding_details():
    records = [
        request("controller-a", scope="unallocated", scope_id="controller-a", input_tokens=10,
                cached_input_tokens=4, output_tokens=2, reasoning_output_tokens=1, total_tokens=12),
        request("controller-b", scope="unallocated", scope_id="controller-b", input_tokens=20,
                cached_input_tokens=5, output_tokens=3, reasoning_output_tokens=1, total_tokens=23),
    ]
    report = build_cost_ledger(records)["report"]
    assert len([row for row in report["by_scope"] if row["scope"] == "unallocated"]) == 2
    summary = report["unallocated_costs"]
    assert summary["recorded_requests"] == 2
    assert summary["scope_ids"] == ["controller-a", "controller-b"]
    assert summary["aggregate"]["token_totals"]["total_tokens"]["known_sum"] == 35
    assert summary["aggregate"]["request_count"] == 2

    coverage = report["coverage_scope"]
    assert coverage["all_task_participants_or_requests_verified"] is False
    assert "supplied entries only" in coverage["audit_status_means"]


@pytest.mark.parametrize("changes", [
    {"input_tokens": True},
    {"output_tokens": -1},
    {"cached_input_tokens": 101},
    {"reasoning_output_tokens": 21},
    {"total_tokens": 121},
    {"actual_credit_debit": 0.1},
])
def test_invalid_token_and_money_values_are_rejected(changes):
    record = request()
    record.update(changes)
    with pytest.raises(LedgerError):
        build_cost_ledger([record])


def test_partial_usage_stays_unknown_and_never_becomes_zero_or_complete():
    record = request(cached_input_tokens=None, total_tokens=None)
    ledger = build_cost_ledger([record], rates())
    report = ledger["report"]
    estimate = ledger["requests"][0]["estimate"]
    assert report["usage_status"] == "partial"
    assert report["token_totals"]["total_tokens"]["known_sum"] is None
    assert report["token_totals"]["total_tokens"]["missing_requests"] == 1
    assert report["non_cached_input_tokens"]["known_sum"] is None
    assert estimate["status"] == "partial"
    assert estimate["estimated_credits_complete"] is False


def test_missing_reasoning_breakdown_does_not_make_core_usage_or_estimate_incomplete():
    ledger = build_cost_ledger([request(reasoning_output_tokens=None)], rates())
    assert ledger["report"]["usage_status"] == "complete"
    assert ledger["report"]["reasoning_breakdown_status"] == "unknown"
    assert ledger["report"]["token_totals"]["reasoning_output_tokens"]["missing_requests"] == 1
    assert ledger["requests"][0]["estimate"]["status"] == "complete"


def test_worker_import_uses_synthetic_thread_sequence_ids_and_single_usage_only():
    fixture = {
        "thread_id": "fixture-thread-001",
        "contexts": [{"model": "gpt-6-luna", "effort": "max", "service_tier": None}],
        "usage": {"input_tokens": 30, "cached_input_tokens": 15, "output_tokens": 3, "total_tokens": 33},
        "request_events": [
            {"request": 1, "phase": "attempt", "outcome": "failed", "failure_kind": "timeout",
             "usage": {"input_tokens": 10, "cached_input_tokens": 5,
                                      "output_tokens": 1, "reasoning_output_tokens": 1,
                                      "cache_write_input_tokens": 2, "total_tokens": 11},
             "total_token_usage": {"input_tokens": 10, "total_tokens": 11}},
            {"request": 2, "phase": "retry", "retry_of": "worker:fixture-thread-001:request:1",
             "usage": {"input_tokens": 20, "cached_input_tokens": 10,
                                      "output_tokens": 2, "reasoning_output_tokens": 2,
                                      "cache_write_input_tokens": 3, "total_tokens": 22},
             "total_token_usage": {"input_tokens": 30, "total_tokens": 33}},
        ],
    }
    records = import_worker_usage(fixture, agent="local", source="fixture:local", scope="experiment",
                                  scope_id="pair-1")
    ledger = build_cost_ledger(records)
    assert [row["request_id"] for row in ledger["requests"]] == [
        "worker:fixture-thread-001:request:1", "worker:fixture-thread-001:request:2",
    ]
    assert all(row["synthetic_request_id"] is True for row in ledger["requests"])
    assert [row["outcome"] for row in ledger["requests"]] == ["failed", None]
    assert ledger["requests"][0]["failure_kind"] == "timeout"
    assert ledger["requests"][1]["retry_of"] == "worker:fixture-thread-001:request:1"
    assert ledger["report"]["request_count"] == 2
    assert all(row["model"] == "gpt-6-luna" and row["speed_mode"] is None for row in ledger["requests"])
    assert all(row["reasoning_effort"] == "max" for row in ledger["requests"])
    assert all(row["speed_mode_source"] is None and row["service_tier"] is None for row in ledger["requests"])
    assert ledger["report"]["token_totals"]["total_tokens"]["known_sum"] == 33
    assert ledger["report"]["token_totals"]["reasoning_output_tokens"]["known_sum"] == 3
    assert ledger["report"]["token_totals"]["cache_write_input_tokens"]["known_sum"] == 5
    assert ledger["report"]["reasoning_breakdown_status"] == "complete"
    assert ledger["report"]["usage_status"] == "complete"


def test_worker_effort_max_is_never_used_as_speed_mode_for_rate_matching():
    fixture = {
        "thread_id": "fixture-effort-not-speed",
        "contexts": [{"model": "gpt-6-luna", "effort": "max", "service_tier": None}],
        "request_events": [{"request": 1, "usage": {
            "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2,
            "reasoning_output_tokens": 1, "total_tokens": 12,
        }}],
    }
    records = import_worker_usage(fixture, agent="local", source="fixture:local", scope="task")
    ledger = build_cost_ledger(records, rates(model="gpt-6-luna", speed_mode="max"))
    estimate = ledger["requests"][0]["estimate"]
    assert ledger["requests"][0]["reasoning_effort"] == "max"
    assert ledger["requests"][0]["speed_mode"] is None
    assert estimate["status"] == "unknown"
    assert "speed_mode_missing" in estimate["reason"]


@pytest.mark.parametrize("event", [
    {"usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
    {"request": True, "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
    {"request": -1, "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
    {"request": "1", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
    {"request": 1.5, "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
])
def test_worker_events_without_a_reliable_identity_are_rejected(event):
    with pytest.raises(LedgerError, match="request sequence or a nonempty provider_request_id/request_id"):
        import_worker_usage({"thread_id": "fixture", "request_events": [event]})


def test_worker_event_can_use_an_explicit_provider_id_when_sequence_is_missing():
    records = import_worker_usage({
        "thread_id": "fixture",
        "request_events": [{"provider_request_id": "provider-42", "usage": {
            "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2,
        }}],
    })
    assert records[0]["request_id"] == "worker:fixture:provider:provider-42"
    assert records[0]["synthetic_request_id"] is True


def test_cli_writes_machine_readable_output_and_returns_partial_status(tmp_path):
    from scripts.build_cost_ledger import main

    input_path = tmp_path / "fixture.json"
    output_path = tmp_path / "ledger.json"
    input_path.write_text(json.dumps({
        "thread_id": "fixture-thread",
        "contexts": [{"model": "gpt-6-luna", "effort": "max", "service_tier": None}],
        "request_events": [{"request": 1, "usage": {
            "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2, "total_tokens": 12,
        }}],
    }), encoding="utf-8")
    code = main([str(input_path), "--format", "worker-usage", "--agent", "local",
                 "--source", "fixture:local", "--scope", "task", "--scope-id", "task-1",
                 "--output", str(output_path)])
    value = json.loads(output_path.read_text(encoding="utf-8"))
    assert code == 1
    assert value["report"]["audit_status"] == "partial"
    assert value["requests"][0]["request_id"] == "worker:fixture-thread:request:1"
    assert value["report"]["unallocated_costs"]["status"] == "no_unallocated_requests_in_input"
    assert value["report"]["unallocated_costs"]["aggregate"] is None
