import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from remote_workbench import audit
from remote_workbench.audit_web import Handler


@pytest.fixture
def audit_http(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(trust_env=False) as client:
            yield client, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_operation_deeplink_and_parent_child_records(audit_http):
    client, base = audit_http
    parent = audit.create(kind="tool", tool="patch_file", host="example-host", status="finished")
    child = audit.create(kind="command", parent_id=parent, command="git apply --check -", cwd="/tmp/project", status="finished")
    audit.add_child(parent, child)
    audit.update(parent, arguments={"path": "src/app.py"}, diff_kind="submitted_patch",
                 check_only=True, patch_result="checked")

    page = client.get(base + "/", params={"id": parent})
    assert page.status_code == 200
    assert "searchParams.get('id')" in page.text
    assert "selectRecord(id" in page.text

    rows = client.get(base + "/api/records").json()
    assert {parent, child} <= {row["id"] for row in rows}
    parent_data = client.get(base + "/api/record", params={"id": parent}).json()
    child_data = client.get(base + "/api/record", params={"id": child}).json()
    assert parent_data["child_commands"] == [child]
    assert parent_data["diff_kind"] == "submitted_patch"
    assert parent_data["check_only"] is True
    assert parent_data["stream_bytes"] == {}
    assert child_data["command"] == "git apply --check -"
    assert child_data["parent_id"] == parent


def test_parent_record_uses_validated_child_output_and_display_metadata(audit_http):
    client, base = audit_http
    parent = audit.create(kind="tool", tool="git_diff", host="example-host", status="finished",
                          arguments={"mode": "patch", "command": "git diff -- src/app.py"})
    child = audit.create(kind="command", parent_id=parent, command="git diff --no-ext-diff", cwd="/remote/project",
                         status="finished", actual_cwd="/remote/project", exit_code=0,
                         diff_kind="git_patch", check_only=True, patch_result="checked",
                         wrapped_command="zsh -lic 'git diff --no-ext-diff'", output_bytes={"stdout": 22})
    audit.add_child(parent, child)
    audit.update(parent, output_id=child)
    with audit.open_stream(child, "stdout") as stream:
        stream.write(b"@@ -1 +1 @@\n-a\n+b\n")

    value = client.get(base + "/api/record", params={"id": parent}).json()
    assert value["id"] == parent
    assert value["output_id"] == child  # original parent provenance stays intact
    assert value["output_source_id"] == child
    assert value["arguments"]["command"] == "git diff -- src/app.py"
    assert value["stream_bytes"] == {"stdout": len(b"@@ -1 +1 @@\n-a\n+b\n")}
    assert value["actual_cwd"] == "/remote/project"
    assert value["exit_code"] == 0
    assert value["diff_kind"] == "git_patch"
    assert value["check_only"] is True and value["patch_result"] == "checked"
    assert value["wrapped_command"] == "zsh -lic 'git diff --no-ext-diff'"
    output = client.get(base + "/api/output", params={"id": value["output_source_id"], "stream": "stdout"}).json()
    assert output["content"].startswith("@@ -1 +1 @@")


def test_background_submission_view_uses_job_state_not_start_record(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    parent = audit.create(kind="tool", tool="ssh_run_background", status="finished",
                          arguments={"command": "make test"})
    job = audit.create(kind="job", parent_id=parent, status="running", running=True,
                       execution_state="running", sync_state="syncing", exit_code=None,
                       command="make test", job_id="job-1")
    audit.add_child(parent, job)
    audit.update(parent, output_id=job)

    record, source_id = Handler._record_view(parent)
    assert source_id == job
    assert record["status"] == "running"
    assert record["view"]["state"] == "running"
    assert record["view"]["sync_state"] == "syncing"

    audit.update(job, status="finished", running=False, execution_state="finished",
                 sync_state="complete", exit_code=23)
    record, _ = Handler._record_view(parent)
    assert record["view"]["state"] == "failed"


@pytest.mark.parametrize("child_id", ["../outside", "a" * 32])
def test_parent_ignores_invalid_or_missing_output_child(audit_http, child_id):
    client, base = audit_http
    parent = audit.create(kind="tool", tool="exec_command", status="finished")
    audit.update(parent, output_id=child_id, child_commands=[child_id])

    value = client.get(base + "/api/record", params={"id": parent})
    assert value.status_code == 200
    assert value.json()["id"] == parent
    assert value.json()["output_source_id"] == parent
    assert value.json()["stream_bytes"] == {}


def test_parent_does_not_follow_an_unrelated_but_valid_record_id(audit_http):
    client, base = audit_http
    parent = audit.create(kind="tool", tool="exec_command", status="finished")
    unrelated = audit.create(kind="command", command="private command", status="finished")
    audit.update(parent, output_id=unrelated, child_commands=[unrelated])

    value = client.get(base + "/api/record", params={"id": parent}).json()
    assert value["output_source_id"] == parent
    assert value["stream_bytes"] == {}


def test_stream_pages_are_bounded_and_diff_stream_is_readable(audit_http):
    client, base = audit_http
    record = audit.create(kind="command", command="git diff -- src/app.py", status="finished")
    with audit.open_stream(record, "diff") as stream:
        stream.write(b"@@ -1 +1 @@\n-old\n+new\n")
    with audit.open_stream(record, "stdout") as stream:
        stream.write(b"abcdefghij")
    audit.update(record, output_bytes={"diff": 23, "stdout": 10})

    first = client.get(base + "/api/output", params={"id": record, "stream": "stdout", "limit": 4}).json()
    second = client.get(base + "/api/output", params={"id": record, "stream": "stdout", "limit": 4,
                                                         "cursor": first["next_cursor"]}).json()
    diff = client.get(base + "/api/output", params={"id": record, "stream": "diff"}).json()
    assert (first["content"], first["truncated"]) == ("abcd", True)
    assert second["content"] == "efgh"
    assert diff["content"] == "@@ -1 +1 @@\n-old\n+new\n"
    assert diff["total_bytes"] == len(b"@@ -1 +1 @@\n-old\n+new\n")


def test_human_test_summary_uses_complete_saved_log_without_changing_records(audit_http):
    client, base = audit_http
    parent = audit.create(kind="tool", tool="ssh_run", status="finished", host="example-host",
                          arguments={"command": "python3 -m unittest discover -s tests -v"})
    child = audit.create(kind="command", parent_id=parent, command="python3 -m unittest discover -s tests -v",
                         status="finished", exit_code=0, actual_cwd="/remote/test/csvstats", elapsed_ms=2133)
    audit.add_child(parent, child)
    audit.update(parent, output_id=child)
    with audit.open_stream(child, "stderr") as output:
        output.write(b"test_feature ... ok\n" * 30 + b"\nRan 23 tests in 2.133s\n\nOK\n")
    before = (audit.directory(child) / "record.json").read_bytes()

    detail = client.get(base + "/api/record", params={"id": parent}).json()
    assert detail["view"]["state"] == "success"
    assert detail["view"]["title"] == "运行测试"
    assert detail["view"]["workspace"] == "csvstats"
    page = client.get(base + "/api/output", params={"id": child, "stream": "stderr", "limit": 64}).json()
    assert page["truncated"]
    assert page["summary"]["kind"] == "tests" and page["summary"]["count"] == 23
    assert page["summary"]["state"] == "success" and page["summary"]["complete"]
    # Presentation is HTTP-only: the MCP output reader and original records stay unchanged.
    assert "summary" not in audit.read_output(child, "stderr", max_bytes=64)
    assert (audit.directory(child) / "record.json").read_bytes() == before


def test_unfinished_or_oversized_output_does_not_claim_test_success(audit_http):
    client, base = audit_http
    record = audit.create(kind="command", status="running")
    with audit.open_stream(record, "stderr") as output:
        output.write(b"Ran 23 tests in 2.133s\n\nOK\n")
    value = client.get(base + "/api/output", params={"id": record, "stream": "stderr"}).json()
    assert "summary" not in value
    audit.update(record, status="finished", exit_code=0)
    with audit.open_stream(record, "stderr") as output:
        output.write(b"x" * 262145 + b"\nRan 23 tests in 2.133s\n\nOK\n")
    value = client.get(base + "/api/output", params={"id": record, "stream": "stderr"}).json()
    assert "summary" not in value


@pytest.mark.parametrize('worker_op', [None, 'read_lines'])
def test_worker_failure_has_human_summary_but_preserves_raw_output(audit_http, worker_op):
    import json

    client, base = audit_http
    record = audit.create(kind='command', status='finished', exit_code=0, worker_op=worker_op)
    result = {'error': 'file_not_found', 'message': 'File does not exist; create it first.',
              'path': '/remote/project/new.json'}
    raw = '\x1eREMOTE_WORKBENCH_RESULT_' + 'a' * 32 + ':\x1f' + json.dumps(result) + '\n'
    with audit.open_stream(record, 'stdout') as stream:
        stream.write(raw.encode())
    page = client.get(base + '/api/output', params={'id': record, 'stream': 'stdout'}).json()
    assert page['content'] == raw
    assert audit.read_output(record)['content'] == raw
    if worker_op:
        assert page['summary']['kind'] == 'worker_error'
        assert page['summary']['state'] == 'failed'
        assert '/remote/project/new.json' in page['summary']['detail']
        assert 'REMOTE_WORKBENCH_RESULT_' not in page['summary']['detail']
    else:
        assert 'summary' not in page  # Do not interpret arbitrary program output as a worker response.


def test_api_reports_bad_ids_missing_records_streams_and_routes(audit_http):
    client, base = audit_http
    missing = "a" * 32
    assert client.get(base + "/api/record", params={"id": "../secret"}).status_code == 400
    response = client.get(base + "/api/record", params={"id": missing})
    assert response.status_code == 404 and response.json() == {"error": "Operation not found"}
    response = client.get(base + "/api/output", params={"id": missing})
    assert response.status_code == 404
    assert client.get(base + "/api/records", params={"limit": "nope"}).status_code == 400
    record = audit.create(kind="command")
    assert client.get(base + "/api/output", params={"id": record, "stream": "invalid"}).status_code == 400
    assert client.get(base + "/api/unknown").status_code == 404


def test_host_origin_and_untrusted_content_guards(audit_http):
    client, base = audit_http
    hostile = "<script>document.body.innerHTML='owned'</script>"
    record = audit.create(kind="command", command=hostile)
    with audit.open_stream(record, "stdout") as stream:
        stream.write(hostile.encode())
    page = client.get(base + "/", params={"id": record})
    assert page.status_code == 200 and hostile not in page.text
    # The UI uses text nodes for every record, command and output value.
    assert ".textContent" in page.text
    assert ".innerHTML" not in page.text
    assert client.get(base + "/api/record", params={"id": record},
                      headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get(base + "/api/record", params={"id": record},
                      headers={"Host": "evil.example"}).status_code == 403
    assert client.get(base + "/api/output", params={"id": record, "stream": "stdout"}).json()["content"] == hostile


def test_page_exposes_diff_colors_live_refresh_and_selection_preservation(audit_http):
    client, base = audit_http
    page = client.get(base + "/").text
    assert "diff-add" in page and "diff-del" in page and "diff-meta" in page
    assert "renderDiff" in page and "textContent" in page
    assert "setInterval(poll,2000)" in page
    # Record buttons are reconciled by ID on refresh, so the current selection and scroll stay put.
    assert "const buttons = new Map()" in page
    assert "updateSelectionStyle()" in page
    assert "显示内部执行步骤" in page
    assert "技术详情" in page
    assert "运行日志" in page and "代码改动" in page
    assert "[hidden]{display:none!important}" in page
