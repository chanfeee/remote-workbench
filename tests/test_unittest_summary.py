import io
import re
import unittest

from remote_workbench import audit
from remote_workbench.audit_web import Handler
from remote_workbench.compact import compact_result
from remote_workbench.presentation import summarize_output


class _AllSkipped(unittest.TestCase):
    __test__ = False

    @unittest.skip("optional dependency unavailable")
    def test_skipped(self):
        raise AssertionError("skip decorator should prevent the body")


class _PartlySkipped(unittest.TestCase):
    __test__ = False

    def test_a_passes(self):
        self.assertEqual(2 + 2, 4)

    @unittest.skip("not configured")
    def test_b_skips(self):
        raise AssertionError("skip decorator should prevent the body")


class _MixedSubtest(unittest.TestCase):
    __test__ = False

    def test_mixed_subtests(self):
        for item in ("executed", "skipped"):
            with self.subTest(item=item):
                if item == "skipped":
                    self.skipTest("subtest skipped")
                self.assertEqual(item, "executed")


class _MultipleSubtestSkips(unittest.TestCase):
    __test__ = False

    def test_multiple_subtest_skips(self):
        for item in ("executed", "skip-one", "skip-two"):
            with self.subTest(item=item):
                if item.startswith("skip"):
                    self.skipTest(item)
                self.assertEqual(item, "executed")


class _ExpectedFailure(unittest.TestCase):
    __test__ = False

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.fail("this failure is expected")


class _UnexpectedSuccess(unittest.TestCase):
    __test__ = False

    @unittest.expectedFailure
    def test_unexpected_success(self):
        self.assertTrue(True)


def _run_unittest(case):
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return stream.getvalue(), result


def test_real_unittest_output_keeps_framework_counts_execution_unknowns_and_process_status(tmp_path):
    cases = (_AllSkipped, _PartlySkipped, _MixedSubtest, _MultipleSubtestSkips,
             _ExpectedFailure, _UnexpectedSuccess)
    projections = {}

    with audit.scoped_root(tmp_path):
        for case in cases:
            output, run = _run_unittest(case)
            process_ok = run.wasSuccessful()
            record = {
                "status": "finished" if process_ok else "failed",
                "execution_state": "finished",
                "exit_code": 0 if process_ok else 1,
            }
            summary = summarize_output(output, record, "stderr", complete=True)
            assert summary is not None, output
            assert summary["tests_run"] == run.testsRun
            assert summary["reported_skips"] == len(run.skipped)
            assert summary["report_status"] == ("success" if process_ok else "failed")
            assert summary["process_status"] == ("success" if process_ok else "failed")

            if run.skipped:
                assert summary["state"] == "completed"
                assert summary["executed"] is None
                assert summary["passed"] is None
            elif run.expectedFailures:
                assert summary["state"] == "completed"
                assert summary["executed"] == run.testsRun
                assert summary["passed"] == run.testsRun - len(run.expectedFailures)
            elif run.unexpectedSuccesses:
                assert summary["state"] == "failed"
                assert summary["executed"] == run.testsRun
                assert summary["passed"] is None
            else:
                assert summary["state"] == "success"
                assert summary["executed"] == summary["passed"] == run.testsRun

            output_id = audit.create(status=record["status"], exit_code=record["exit_code"])
            with audit.open_stream(output_id, "stderr") as saved:
                saved.write(output.encode("utf-8"))

            compacted = compact_result("exec_command", {
                **record,
                "stdout": "",
                "stderr": output,
                "timed_out": False,
                "truncated": False,
                "output_id": output_id,
            }, {"command": "python3 -m unittest -v"})
            compact_summary = compacted.get("test_summary")
            assert compacted["output_id"] == output_id
            if compact_summary is None:
                # Older unittest emits a standalone continuation for a second
                # skipped subtest. Compact deliberately retains unrecognized
                # progress text rather than hiding it behind a footer summary.
                assert case is _MultipleSubtestSkips, output
                assert re.search(r"(?m)^[ \t]*skipped 'skip-two'$", output), output
                assert compacted["stderr"] == output
            else:
                for field in (
                    "tests_run", "reported_skips", "executed", "passed",
                    "report_status", "process_status",
                ):
                    assert compact_summary[field] == summary[field]
                assert compact_summary["status"] == ("passed" if summary["state"] == "success" else summary["state"])

            html_summary = Handler._output_summary(output_id, "stderr")
            assert html_summary == summary
            assert audit.read_output(output_id, "stderr", max_bytes=65536)["content"] == output

            # Summaries supplement the budgeted execution report. Even a
            # successful report remains visible without a second log read.
            assert compacted["stderr"] == output
            projections[case] = (output, summary)

        # A normal skip and a mixed subTest pass/skip have the same unittest
        # footer and progress marker. The summary must not guess which ran.
        ordinary_output, ordinary = projections[_AllSkipped]
        subtest_output, subtest = projections[_MixedSubtest]
        footer_one_skip = re.compile(r"(?m)^Ran 1 test in [0-9.]+s\n\nOK \(skipped=1\)$")
        assert footer_one_skip.search(ordinary_output)
        assert footer_one_skip.search(subtest_output)
        for field in ("tests_run", "reported_skips", "executed", "passed", "state"):
            assert ordinary[field] == subtest[field]
        assert ordinary["executed"] is ordinary["passed"] is None

        many_skips_output, many_skips = projections[_MultipleSubtestSkips]
        assert re.search(r"(?m)^Ran 1 test in [0-9.]+s\n\nOK \(skipped=2\)$", many_skips_output)
        assert many_skips["tests_run"] == 1 and many_skips["reported_skips"] == 2
        assert many_skips["executed"] is many_skips["passed"] is None

        expected = projections[_ExpectedFailure][1]
        assert expected["expected_failures"] == 1
        assert expected["executed"] == 1 and expected["passed"] == 0

        unexpected = projections[_UnexpectedSuccess][1]
        assert unexpected["unexpected_successes"] == 1
        assert unexpected["report_status"] == unexpected["process_status"] == "failed"
        assert unexpected["passed"] is None
