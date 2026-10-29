import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.tool_interface import TOOL_NAMES, present_result
from remote_workbench.session_server import create_server
from tests.test_session_server import call, project as project, router_at


def result(**updates):
    return dict({"stdout": "", "stderr": "test_one (tests.Tests) ... ok\n\n----------------------------------------------------------------------\nRan 1 test in 0.001s\n\nOK\n",
                 "exit_code": 0, "timed_out": False, "truncated": False,
                 "execution_state": "finished", "output_id": "a" * 32}, **updates)


def test_only_plain_complete_report_is_summarized_without_hiding_output():
    source = result()
    value = present_result("exec_command", source, {"command": "python3 -m unittest -v"})
    assert value["test_summary"] == {
        "status": "passed", "tests": 1, "tests_run": 1, "reported_skips": 0,
        "executed": 1, "passed": 1, "report_status": "success", "process_status": "success",
        "total": 1, "skipped": 0,
    }
    assert value["stderr"] == source["stderr"] and value["exit_code"] == 0
    assert source["stderr"]  # projection does not mutate audit/source data
    failed_but_recognized = present_result(
        "exec_command", dict(source, exit_code=1), {"command": "python3 -m unittest -v"}
    )
    assert failed_but_recognized["test_summary"]["status"] == "failed"
    assert failed_but_recognized["test_summary"]["process_status"] == "failed"
    assert failed_but_recognized["stderr"] == source["stderr"]
    failed_footer = present_result(
        "exec_command", dict(source, stderr=source["stderr"].replace("OK", "FAILED (failures=1)")),
        {"command": "python3 -m unittest -v"},
    )
    assert failed_footer["test_summary"]["report_status"] == "failed"
    assert failed_footer["stderr"].endswith("FAILED (failures=1)\n")
    for changes in ({"timed_out": True}, {"execution_state": "remote_state_unknown"},
                    {"stderr": "warning: useful details\n" + source["stderr"]},
                    {"stdout": "fixture diagnostics\n"}, {"truncated": True}):
        changed = dict(source, **changes)
        visible = present_result("exec_command", changed, {"command": "python3 -m unittest -v"})
        assert "test_summary" not in visible
        assert visible["stderr"] == changed["stderr"]
    assert "test_summary" not in present_result("exec_command", source, {"command": "cat saved-test.txt"})


def test_large_success_uses_full_spool_but_keeps_unknown_output(tmp_path):
    with audit.scoped_root(tmp_path):
        output_id = audit.create(kind="command", status="finished")
        path = audit.directory(output_id)
        text = "test_one (tests.Tests) ... ok\n" * 100 + "\n----------------------------------------------------------------------\nRan 100 tests in 0.001s\n\nOK\n"
        (path / "stdout").write_text("")
        (path / "stderr").write_text(text)
        original = result(truncated=True, output_id=output_id, stderr="head/tail preview")
        value = present_result("exec_command", original, {"command": "python3 -m unittest -v"})
        assert value["test_summary"]["tests"] == 100
        assert value["truncated"] is True
        assert value["stderr"] == "head/tail preview"
        assert (path / "stderr").read_text() == text
        (path / "stderr").write_text("ResourceWarning: do not hide this\n" + text)
        assert "test_summary" not in present_result("exec_command", original, {"command": "python3 -m unittest -v"})


def test_legacy_subtest_skip_continuation_keeps_complete_report_visible():
    output = (
        "test_subtests (tests.Example) ... skipped 'skip-one'\n"
        "skipped 'skip-two'\n\n"
        "----------------------------------------------------------------------\n"
        "Ran 1 test in 0.001s\n\nOK (skipped=2)\n"
    )
    value = present_result("exec_command", result(stderr=output),
                           {"command": "python3 -m unittest -v"})
    assert "test_summary" not in value
    assert value["stderr"] == output
    assert value["exit_code"] == 0
    assert value["output_id"] == "a" * 32


def test_skipped_unittest_counts_are_explicit_and_all_skipped_is_not_passed():
    skipped = result(
        stderr=("test_skipped (tests.Example) ... skipped 'missing optional environment'\n\n"
                "----------------------------------------------------------------------\n"
                "Ran 1 test in 0.001s\n\nOK (skipped=1)\n"),
    )
    value = present_result("exec_command", skipped, {"command": "python3 -m unittest -v"})
    assert value["test_summary"] == {
        "status": "completed", "tests": None, "tests_run": 1, "reported_skips": 1,
        "executed": None, "passed": None, "report_status": "success", "process_status": "success",
        "total": 1, "skipped": 1,
    }
    assert value["stderr"] == skipped["stderr"]
    assert skipped["stderr"].startswith("test_skipped")

    partial = result(stderr=("test_one (tests.Example) ... ok\n"
                             "test_two (tests.Example) ... skipped 'not configured'\n\n"
                             "----------------------------------------------------------------------\n"
                             "Ran 2 tests in 0.002s\n\nOK (skipped=1)\n"))
    value = present_result("exec_command", partial, {"command": "python3 -m unittest -v"})
    assert value["test_summary"] == {
        "status": "completed", "tests": None, "tests_run": 2, "reported_skips": 1,
        "executed": None, "passed": None, "report_status": "success", "process_status": "success",
        "total": 2, "skipped": 1,
    }
    assert value["stderr"] == partial["stderr"]


def test_nonverbose_skips_expected_failures_unknown_counters_and_failures():
    command = {"command": "python3 -m unittest"}
    nonverbose = result(stderr="s\nRan 1 test in 0.001s\n\nOK (skipped=1)\n")
    assert present_result("exec_command", nonverbose, command)["test_summary"] == {
        "status": "completed", "tests": None, "tests_run": 1, "reported_skips": 1,
        "executed": None, "passed": None, "report_status": "success", "process_status": "success",
        "total": 1, "skipped": 1,
    }

    expected = result(stderr=("test_expected (tests.Example) ... expected failure\n\n"
                              "----------------------------------------------------------------------\n"
                              "Ran 1 test in 0.001s\n\nOK (expected failures=1)\n"))
    assert present_result("exec_command", expected, {"command": "python3 -m unittest -v"})["test_summary"] == {
        "status": "completed", "tests": 0, "tests_run": 1, "reported_skips": 0,
        "executed": 1, "passed": 0, "report_status": "success", "process_status": "success",
        "total": 1, "skipped": 0, "expected_failures": 1,
    }

    unexpected = result(stderr="Ran 1 test in 0.001s\n\nFAILED (unexpected successes=1)\n")
    visible = present_result("exec_command", unexpected, command)
    assert visible["test_summary"] == {
        "status": "failed", "tests": None, "tests_run": 1, "reported_skips": 0,
        "executed": 1, "passed": None, "report_status": "failed", "process_status": "success",
        "total": 1, "skipped": 0, "unexpected_successes": 1,
    }
    assert visible["stderr"] == unexpected["stderr"]

    unknown = result(stderr="s\nRan 1 test in 0.001s\n\nOK (skipped=1, future counter=1)\n")
    visible = present_result("exec_command", unknown, command)
    assert "test_summary" not in visible and visible["stderr"] == unknown["stderr"]

    nonzero = result(exit_code=1, stderr="Ran 1 test in 0.001s\n\nOK (skipped=1)\n")
    visible = present_result("exec_command", nonzero, command)
    assert visible["test_summary"]["status"] == "failed"
    assert visible["test_summary"]["process_status"] == "failed"
    assert visible["test_summary"]["report_status"] == "success"
    assert visible["stderr"] == nonzero["stderr"]

    warning = result(stderr="warning: optional dependency unavailable\n" + nonverbose["stderr"])
    visible = present_result("exec_command", warning, command)
    assert "test_summary" not in visible and visible["stderr"] == warning["stderr"]


def test_truncated_skipped_result_uses_complete_unchanged_spool(tmp_path):
    with audit.scoped_root(tmp_path):
        output_id = audit.create(kind="command", status="finished")
        path = audit.directory(output_id)
        text = "s\nRan 1 test in 0.001s\n\nOK (skipped=1)\n"
        (path / "stdout").write_text("")
        (path / "stderr").write_text(text)
        original = result(truncated=True, output_id=output_id, stderr="partial preview")
        value = present_result("exec_command", original, {"command": "python3 -m unittest"})
        assert value["test_summary"]["status"] == "completed"
        assert (path / "stderr").read_text() == text

        # An incomplete or unavailable spool keeps the preview and its warning.
        original["output_id"] = "not-an-output-id"
        visible = present_result("exec_command", original, {"command": "python3 -m unittest"})
        assert "test_summary" not in visible and visible["stderr"] == "partial preview"


def test_cleanup_uncertainty_ids_and_error_diagnostics_survive():
    task = {"state": "finished", "reason": "finished", "cleanup_state": "unconfirmed", "tree_confirmed": False,
            "pgid": 123, "node": "node1", "message": "cleanup unavailable"}
    source = {"error": "failed", "message": "actionable", "execution_state": "remote_state_unknown",
              "remote_task": task, "output_id": "a" * 32, "operation_id": "b" * 32,
              "unknown_future_field": "retained"}
    value = present_result("exec_command", source)
    assert value["remote_task"] == task
    assert value["unknown_future_field"] == "retained"
    assert value["error"] == "failed" and value["message"] == "actionable"
    assert value["output_id"] == "a" * 32


def test_normal_cleanup_limits_remain_explicit_without_repeating_origin():
    source = result(remote_task={"state": "finished", "reason": "finished", "cleanup_state": "process_group_signalled",
                    "tree_confirmed": False, "hard_process_limit": False, "node": "node1", "pgid": 123,
                    "future_cleanup_field": "keep"},
                    test_evidence={"status": "passed", "applies_to_current_code": True,
                                   "environment_evidence": "command_and_host_configuration_only",
                                   "environment_limit": "not fingerprinted", "future_evidence": "keep"})
    value = present_result("exec_command", source)
    assert value["remote_task"]["tree_confirmed"] is False
    assert value["remote_task"]["hard_process_limit"] is False
    assert value["remote_task"]["future_cleanup_field"] == "keep"
    assert "pgid" not in value["remote_task"]
    assert value["test_evidence"] == source["test_evidence"]
    failed = present_result("exec_command", dict(source, exit_code=1))
    assert failed["remote_task"]["pgid"] == 123


async def test_single_catalog_keeps_advanced_tools_available_on_demand(tmp_path):
    router = router_at(tmp_path)
    compact = create_server(router)
    tools = await compact.list_tools()
    assert {t.name for t in tools} == TOOL_NAMES | {"workspace_tools"}
    assert all(t.outputSchema is None for t in tools)
    encode = lambda tools: json.dumps([t.model_dump(exclude_none=True) for t in tools]).encode()
    # A byte ceiling is not a model-request or token-cost guarantee. Keep one
    # advertised interface as command/patch capabilities grow.
    assert len(tools) == len(TOOL_NAMES) + 1
    for old_name in ("full", "compact"):
        legacy = create_server(router, tool_profile=old_name)
        assert encode(await legacy.list_tools()) == encode(tools)
        assert legacy._mcp_server.instructions == compact._mcp_server.instructions
        assert await call(legacy, "parity", "workspace_info") == await call(compact, "parity", "workspace_info")
    described = await call(compact, "a", "workspace_tools", tool="describe", arguments={"name": "start_job"})
    assert described["name"] == "start_job" and "command" in described["inputSchema"]["properties"]
    assert "threadId" not in described["inputSchema"]["properties"]
    saved = await call(compact, "a", "workspace_tools", tool="workspace_note", arguments={"goal": "keep this chat separate"})
    assert saved["updated"]
    resumed = await call(compact, "a", "workspace_tools", tool="workspace_resume")
    assert resumed["note"]["goal"] == "keep this chat separate"
    other = await call(compact, "b", "workspace_tools", tool="workspace_resume")
    assert other["note"] is None
    assert not router._executors
    with pytest.raises(ToolError, match="threadId"):
        await call(compact, None, "workspace_tools", tool="workspace_resume")


async def test_switch_can_inspect_actual_directory_and_preserves_binding_after_failure(project):
    router, _, source, _ = project
    compact = create_server(router, viewer_url="http://127.0.0.1:9999")
    opened = await call(compact, "a", "workspace_switch", host="a", cwd=str(source), command="cat value.txt")
    assert opened["initial"]["stdout"] == "original\n"
    assert opened["cwd"] == str(source)
    assert "?id=" not in opened["operation_log"]
    assert opened["operation_log"].endswith(f"/w/{router.registry.get_or_create('a').workspace_id}/)")
    other = await call(compact, "b", "workspace_switch", host="a", cwd=str(source), command="pwd")
    assert other["cwd"] == opened["cwd"]
    # Chats may explicitly share the same actual code directory.
    edited = await call(compact, "a", "exec_command", command="printf edited > value.txt")
    assert edited["exit_code"] == 0
    assert (source / "value.txt").read_text() == "edited"
    assert Path(other["cwd"], "value.txt").read_text() == "edited"
    failed = await call(compact, "a", "workspace_switch", host="a", cwd=str(source), command="exit 3")
    assert failed["bound"] and failed["initial"]["exit_code"] == 3
    assert router.registry.get_or_create("a").cwd == opened["cwd"]


async def test_advanced_routing_retains_validation_and_no_local_fallback(project):
    router, _, source, _ = project
    compact = create_server(router)
    with pytest.raises(ToolError, match="no remote workspace"):
        await call(compact, "new-chat", "workspace_tools", tool="write_file", arguments={"path": "value.txt", "content": "bad"})
    await call(compact, "a", "workspace_switch", host="a", cwd=str(source))
    with pytest.raises(ToolError):
        await call(compact, "a", "workspace_tools", tool="read_file", arguments={"path": "value.txt", "limit": "not-an-integer"})
    with pytest.raises(ToolError):
        await call(compact, "a", "workspace_tools", tool="read_file", arguments={"path": "value.txt", "ctx": {}})
    assert (source / "value.txt").read_text() == "original\n"


def test_invalid_profile_rejected_before_serving(tmp_path):
    with pytest.raises(ValueError, match="tool_profile"):
        create_server(router_at(tmp_path), tool_profile="unknown")



def test_explicit_audit_read_keeps_complete_original_metadata():
    record = {"workspace_id":"original", "revision":7, "operation_id":"parent", "output_id":"child",
              "execution_state":"finished", "exit_code":0,
              "remote_task":{"state":"finished", "reason":"finished", "cleanup_state":"process_group_signalled",
                             "node":"original-node", "pid":123, "pgid":123, "tree_confirmed":False}}
    assert present_result("audit_get", record) == record


def test_session_help_has_no_profile_selection():
    import subprocess
    import sys
    result = subprocess.run([sys.executable,"-m","remote_workbench.session_server","--help"],
                            capture_output=True,text=True,timeout=10,check=True)
    assert "--tool-profile" not in result.stdout
    assert "--data-dir" in result.stdout



def test_old_module_is_only_an_import_alias():
    from remote_workbench import compact, tool_interface
    assert compact.compact_result is tool_interface.present_result
    assert compact.COMPACT_INSTRUCTIONS is tool_interface.INSTRUCTIONS
    assert compact.COMPACT_TOOLS is tool_interface.TOOL_NAMES
