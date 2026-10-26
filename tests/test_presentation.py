from remote_workbench.presentation import parse_unified_diff, summarize_output, summarize_record


def test_record_summary_uses_tool_args_and_real_command_source():
    view = summarize_record(
        {
            "kind": "tool",
            "tool": "ssh_run",
            "arguments": {"command": "python -m unittest discover -v", "cwd": "~/work/csvstats"},
            "host": "example-host",
            "status": "finished",
            "elapsed_ms": 2133,
        },
        {
            "kind": "command",
            "command": "python -m unittest discover -v",
            "actual_cwd": "/home/user/work/csvstats",
            "host": "tester@example-host",
            "status": "finished",
            "exit_code": 0,
        },
    )
    assert view == {
        "title": "运行测试",
        "state": "success",
        "state_label": "成功",
        "host": "example-host",
        "workspace": "csvstats",
        "workspace_path": "/home/user/work/csvstats",
        "elapsed": "2.133 秒",
        "command": "python -m unittest discover -v",
    }


def test_record_summary_does_not_call_stderr_failure_or_hide_nonzero_exit():
    successful_stderr = summarize_record(
        {"tool": "ssh_run", "arguments": {"command": "pytest"}, "status": "finished", "exit_code": 0}
    )
    failed_exit = summarize_record(
        {
            "tool": "ssh_run",
            "arguments": {"command": "python app.py"},
            "status": "finished",
            "exit_code": 4,
            "stderr": "warning only",
        }
    )
    assert successful_stderr["state"] == "success"
    assert failed_exit["state"] == "failed"
    assert failed_exit["title"] == "执行命令"


def test_record_summary_maps_running_timeout_error_and_checked_states():
    assert summarize_record({"status": "running"})["state"] == "running"
    assert summarize_record({"status": "timeout", "exit_code": -1})["state"] == "timeout"
    assert summarize_record({"status": "finished", "error": "git_failed", "exit_code": 0})["state"] == "failed"
    assert summarize_record({"status": "finished", "check_only": True, "exit_code": 0})["state"] == "checked"
    assert summarize_record({"status": "cancelled", "execution_state": "remote_state_unknown"})["state"] == "unknown"


def test_record_summary_hides_python_worker_wrapper():
    view = summarize_record(
        {
            "tool": "ssh_patch_file",
            "arguments": {"path": "src/stats.py"},
            "worker_op": "replace",
            "command": "python3 -c <serialized worker request>",
            "cwd": "/srv/project",
        }
    )
    assert view["title"] == "修改文件：src/stats.py"
    assert view["command"] == "修改文件：src/stats.py"
    assert view["workspace"] == "project"
    assert "python3" not in view["command"]


def test_unittest_success_summary_requires_complete_valid_footer_and_zero_exit():
    output = "...\nRan 3 tests in 0.012s\n\nOK\n"
    record = {"status": "finished", "exit_code": 0}
    assert summarize_output(output, record, "stderr", complete=False) is None
    assert summarize_output(output + "warning after summary\n", record, "stdout", complete=True) is None
    assert summarize_output(output, {"status": "finished", "exit_code": 1}, "stdout", complete=True)["state"] == "failed"
    result = summarize_output(output, record, "stderr", complete=True)
    assert result == {
        "kind": "tests",
        "title": "3 项测试通过",
        "state": "success",
        "count": 3,
        "tests_run": 3,
        "reported_skips": 0,
        "executed": 3,
        "passed": 3,
        "report_status": "success",
        "process_status": "success",
        "detail": "耗时 0.012 秒",
        "complete": True,
        "total": 3,
        "skipped": 0,
    }


def test_unittest_failure_and_subtest_errors_are_retained():
    output = (
        "FAIL: test_totals (tests.test_stats.StatsTests.test_totals) (year=2024)\n"
        "AssertionError: mismatch\n"
        "ERROR: test_parse (tests.test_stats.StatsTests.test_parse)\n"
        "Ran 2 tests in 0.200s\n\nFAILED (failures=1, errors=1)\n"
    )
    result = summarize_output(output, {"status": "finished", "exit_code": 1}, "stdout", complete=True)
    assert result["state"] == "failed"
    assert result["title"] == "测试未通过（共 2 项）"
    assert result["detail"] == "耗时 0.200 秒；FAILED (failures=1, errors=1)"
    assert result["failures"] == [
        "test_totals (tests.test_stats.StatsTests.test_totals) (year=2024)",
        "test_parse (tests.test_stats.StatsTests.test_parse)",
    ]
    assert result["failure_count"] == 2


def test_unittest_success_needs_exit_code_and_finished_record():
    output = "Ran 1 test in 0.001s\n\nOK\n"
    assert summarize_output(output, {"status": "finished"}, "stdout", complete=True) is None
    assert summarize_output(output, {"status": "running", "exit_code": 0}, "stdout", complete=True) is None
    assert summarize_output(output, {"status": "timeout", "exit_code": 0}, "stdout", complete=True) is None


def test_unittest_skipped_counts_do_not_guess_subtest_execution_semantics():
    record = {"status": "finished", "exit_code": 0}
    all_skipped = summarize_output(
        "test_skipped (tests.Example) ... skipped 'missing optional environment'\n\n"
        "----------------------------------------------------------------------\nRan 1 test in 0.001s\n\nOK (skipped=1)\n",
        record, "stderr", complete=True,
    )
    assert all_skipped["state"] == "completed"
    assert all_skipped["tests_run"] == all_skipped["count"] == all_skipped["total"] == 1
    assert all_skipped["reported_skips"] == all_skipped["skipped"] == 1
    assert all_skipped["executed"] is all_skipped["passed"] is None
    assert all_skipped["process_status"] == "success"

    partial = summarize_output(
        "s.\nRan 2 tests in 0.002s\n\nOK (skipped=1)\n",
        record, "stdout", complete=True,
    )
    assert partial["state"] == "completed"
    assert partial["tests_run"] == partial["count"] == partial["total"] == 2
    assert partial["reported_skips"] == partial["skipped"] == 1
    assert partial["executed"] is partial["passed"] is None

    ordinary = summarize_output("Ran 3 tests in 0.012s\n\nOK\n", record, "stderr", complete=True)
    assert ordinary == {
        "kind": "tests", "title": "3 项测试通过", "state": "success", "count": 3,
        "tests_run": 3, "reported_skips": 0, "executed": 3, "passed": 3,
        "report_status": "success", "process_status": "success",
        "detail": "耗时 0.012 秒", "complete": True, "total": 3, "skipped": 0,
    }


def test_unittest_expected_failures_are_counted_and_unknown_footer_is_not_summarized():
    expected_failure = summarize_output(
        "test_expected (tests.Example) ... expected failure\n\n"
        "----------------------------------------------------------------------\nRan 1 test in 0.001s\n\nOK (expected failures=1)\n",
        {"status": "finished", "exit_code": 0}, "stderr", complete=True,
    )
    assert expected_failure["title"] == "测试报告完成（框架运行 1 项，预期失败 1 项，执行 1 项，通过 0 项）"
    assert expected_failure["state"] == "completed"
    assert expected_failure["expected_failures"] == 1
    assert expected_failure["passed"] == 0 and expected_failure["executed"] == 1
    assert expected_failure["process_status"] == "success"

    mixed = summarize_output(
        ".s.\nRan 3 tests in 0.001s\n\nOK (skipped=1, expected failures=1)\n",
        {"status": "finished", "exit_code": 0}, "stderr", complete=True,
    )
    assert mixed["tests_run"] == mixed["total"] == 3
    assert mixed["reported_skips"] == mixed["skipped"] == 1
    assert mixed["executed"] is mixed["passed"] is None
    assert mixed["expected_failures"] == 1
    assert "执行数和通过数未知" in mixed["title"]

    unexpected_success = summarize_output(
        "Ran 1 test in 0.001s\n\nFAILED (unexpected successes=1)\n",
        {"status": "finished", "exit_code": 1}, "stderr", complete=True,
    )
    assert unexpected_success["state"] == "failed"
    assert unexpected_success["unexpected_successes"] == 1
    assert unexpected_success["executed"] == 1 and unexpected_success["passed"] is None
    assert unexpected_success["report_status"] == unexpected_success["process_status"] == "failed"
    assert summarize_output(
        "s\nRan 1 test in 0.001s\n\nOK (skipped=1, future counter=1)\n",
        {"status": "finished", "exit_code": 0}, "stderr", complete=True,
    ) is None
    many_subtest_skips = summarize_output(
        "Ran 1 test in 0.001s\n\nOK (skipped=2)\n",
        {"status": "finished", "exit_code": 0}, "stderr", complete=True,
    )
    assert many_subtest_skips["tests_run"] == 1
    assert many_subtest_skips["reported_skips"] == 2
    assert many_subtest_skips["executed"] is many_subtest_skips["passed"] is None
    assert summarize_output(
        "Ran 0 tests in 0.001s\n\nOK\n",
        {"status": "finished", "exit_code": 0}, "stderr", complete=True,
    ) is None


def test_failed_unittest_footer_never_derives_passed_from_other_counts():
    mixed = summarize_output(
        "FAIL: test_one (tests.Example)\nAssertionError: mismatch\n"
        "Ran 3 tests in 0.001s\n\nFAILED (failures=1, skipped=1)\n",
        {"status": "finished", "exit_code": 1}, "stderr", complete=True,
    )
    assert mixed["state"] == "failed"
    assert mixed["tests_run"] == mixed["total"] == 3
    assert mixed["reported_skips"] == mixed["skipped"] == 1
    assert mixed["executed"] is None and mixed["passed"] is None
    assert mixed["reported_failures"] == 1
    assert mixed["passed"] is None

    # Failure counts may count several failing subtests inside a single test;
    # they cannot be subtracted from Ran N to infer the passing test count.
    subtests = summarize_output(
        "FAIL: test_many (tests.Example) (case=1)\nAssertionError: first\n"
        "FAIL: test_many (tests.Example) (case=2)\nAssertionError: second\n"
        "Ran 2 tests in 0.001s\n\nFAILED (failures=2, expected failures=1)\n",
        {"status": "finished", "exit_code": 1}, "stderr", complete=True,
    )
    assert subtests["reported_failures"] == 2
    assert subtests["expected_failures"] == 1
    assert subtests["tests_run"] == subtests["total"] == 2 and subtests["executed"] == 2
    assert subtests["failure_count"] == 2
    assert subtests["passed"] is None


def test_diff_summary_ignores_git_headers_and_counts_only_hunk_lines():
    patch = (
        "diff --git a/src/stats.py b/src/stats.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/stats.py\n"
        "+++ b/src/stats.py\n"
        "@@ -1,3 +1,4 @@\n"
        " old = 1\n"
        "-value = old\n"
        "--- this is deleted source, not a header\n"
        "+value = old + 1\n"
        "+++ this is added source, not a header\n"
        "+print(value)\n"
    )
    result = parse_unified_diff(patch, complete=True)
    assert result == {
        "kind": "diff",
        "title": "共 1 个文件变更，新增 3 行、删除 2 行",
        "files": [{"path": "src/stats.py", "added": 3, "deleted": 2}],
        "added": 3,
        "deleted": 2,
        "complete": True,
    }


def test_diff_summary_handles_add_delete_and_quoted_filename():
    patch = (
        'diff --git "a/new file.py" "b/new file.py"\n'
        "new file mode 100644\n"
        "--- /dev/null\n+++ \"b/new file.py\"\n"
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-gone\n"
    )
    result = parse_unified_diff(patch, complete=True)
    assert result["files"] == [
        {"path": "new file.py", "added": 2, "deleted": 0},
        {"path": "old.py", "added": 0, "deleted": 1},
    ]
    assert result["added"] == 2
    assert result["deleted"] == 1


def test_partial_diff_is_marked_and_non_diff_or_combined_diff_is_omitted():
    partial = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        complete=False,
    )
    assert partial["complete"] is False
    assert "片段" in partial["title"]
    assert partial["added"] == 1 and partial["deleted"] == 1
    assert parse_unified_diff("ordinary command output\n", complete=True) is None
    assert parse_unified_diff("diff --git a/a b/a\n@@@ -1 +1 @@@\n", complete=True) is None


def test_background_submission_success_does_not_mean_job_completed():
    parent = {'tool': 'ssh_run_background', 'status': 'finished', 'arguments': {'command': 'sleep 1'}}
    job = {'kind': 'job', 'status': 'running', 'running': True, 'sync_state': 'syncing'}
    view = summarize_record(parent, job)
    assert view['state'] == 'running'
    assert view['sync_label'] == '日志同步中'
    job.update(status='unknown', running=None, sync_state='disconnected', execution_state='unknown')
    view = summarize_record(parent, job)
    assert view['state'] == 'unknown'
    assert '待核实' in view['sync_label']


def test_unknown_cleanup_does_not_become_success_from_zero_exit_or_test_footer():
    record = {'kind': 'job', 'status': 'finished', 'exit_code': 0,
              'execution_state': 'unknown', 'sync_state': 'complete'}
    assert summarize_record(record)['state'] == 'unknown'
    assert summarize_output('Ran 1 test in 0.001s\n\nOK\n', record, 'stdout', complete=True) is None
