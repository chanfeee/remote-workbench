from copy import deepcopy

import pytest

from scripts.check_task_budget import compare


def arm(total, **fields):
    return dict({"model": "same-model", "reasoning_effort": "max", "requests": 9,
                 "baseline_sha256": "0" * 64, "task_sha256": "1" * 64,
                 "acceptance_passed": True, "complete": True,
                 "usage": {"input_tokens": total - 100, "cached_input_tokens": 100,
                           "output_tokens": 100, "total_tokens": total}}, **fields)


def test_max_two_percent_boundary_counts_cached_input_once():
    assert compare(arm(10000), arm(10199))["result"] == "sample_within_budget"
    boundary = compare(arm(10000), arm(10200))
    assert boundary["result"] == "sample_within_budget"
    assert boundary["threshold_percent_max"] == "2" and boundary["threshold_comparison"] == "<="
    assert compare(arm(10000), arm(10201))["result"] == "not_qualified"


@pytest.mark.parametrize("change", [{"requests": 10}, {"model": "different"},
                                   {"baseline_sha256": "2" * 64}, {"task_sha256": "3" * 64},
                                   {"reasoning_effort": "different"}, {"complete": False},
                                   {"acceptance_passed": False}])
def test_a_cheap_but_invalid_pair_cannot_pass(change):
    assert compare(arm(10000), arm(9000, **change))["result"] == "not_qualified"


def test_invalid_usage_cannot_double_count_cache_or_omit_output():
    source = arm(10000)
    for changes in ({"cached_input_tokens": 20000}, {"input_tokens": 10000},
                    {"total_tokens": True}, {"output_tokens": -1}):
        invalid = deepcopy(source)
        invalid["usage"].update(changes)
        with pytest.raises(ValueError):
            compare(source, invalid)


def test_negative_delta_is_reported_as_one_sample_without_quota_claim():
    value = compare(arm(10000), arm(9000))
    assert value["additional_percent_of_native"] == "-10"
    assert value["account_allowance_delta"] is None
    assert "not a guarantee" in value["limitation"]
