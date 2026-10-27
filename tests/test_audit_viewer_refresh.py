"""Exercise the shipped viewer script with deterministic polling and HTTP races."""
from pathlib import Path
from html.parser import HTMLParser
import shutil
import subprocess

import pytest

from remote_workbench.audit_web import HTML


@pytest.mark.parametrize("case", [
    "follow_latest", "initially_empty", "final_tail", "late_stdout", "late_stderr",
    "manual_history", "raw_output", "new_output_source", "restored_checkbox", "retry",
    "manual_refresh", "pinned_stream", "retry_record", "viewer_valid", "viewer_foreign",
    "viewer_missing", "default_live", "disable_default_live", "worker_error",
])
def test_live_refresh_behaviour(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for browser-script behaviour tests")
    script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    args = [node, str(Path(__file__).with_name("audit_viewer_refresh.cjs")), case]
    viewer_id = response_viewer = ""
    if case.startswith("viewer_"):
        viewer_id = "e" * 32
        script = script.replace("const VIEWER_ID = null;", f'const VIEWER_ID = "{viewer_id}";', 1)
        response_viewer = {
            "viewer_valid": viewer_id,
            "viewer_foreign": "f" * 32,
            "viewer_missing": "-",
        }[case]
    class Controls(HTMLParser):
        live_checked = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("id") == "live":
                self.live_checked = "checked" in attrs

    controls = Controls()
    controls.feed(HTML)
    args.extend([viewer_id, response_viewer, str(controls.live_checked).lower()])
    result = subprocess.run(
        args,
        input=script, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
