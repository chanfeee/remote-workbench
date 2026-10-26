from remote_workbench.test_evidence import observation, compare, invalidate


def point(digest='abc', complete=True, config='env'):
    return {'code': {'complete': complete, 'digest': digest}, 'config_digest': config}


def unittest_result(stdout, exit_code=0):
    return {'stdout': stdout, 'stderr': '', 'exit_code': exit_code, 'execution_state': 'finished'}


def test_result_is_historical_when_code_changes_and_environment_never_certified():
    value = observation(point(), point(), unittest_result('Ran 2 tests in 0.001s\n\nOK\n'), 'pytest', 1)
    assert value['status'] == 'passed'
    assert value['process_exit_status'] == 'success'
    assert value['test_result'] == 'success'
    assert value['tests_run'] == 2 and value['executed'] == 2 and value['passed'] == 2
    assert compare(value, point())['applies_to_current_code'] is True
    changed = compare(value, point('def'))
    assert changed['historical'] is True
    assert changed['applies_to_current_code'] is False
    assert changed['environment_verified_current'] is False


def test_changed_during_test_or_incomplete_scan_never_certifies_current_code():
    for before, after in ((point(), point('def')), (point(complete=False), point())):
        value = observation(before, after, unittest_result('Ran 2 tests in 0.001s\n\nOK\n'), 'pytest', 1)
        assert value['code_unchanged_during_test'] is False
        assert compare(value, after)['applies_to_current_code'] is None
    value = observation(point(), point(), {'execution_state': 'unknown', 'exit_code': 0}, 'pytest', 1)
    assert value['status'] == 'unknown'
    assert value['test_result'] == 'unknown' and value['reliable_completion'] is False
    assert value['applies_to_current_code'] is None
    assert compare(value, point())['applies_to_current_code'] is None
    assert compare(value, point(complete=False))['applies_to_current_code'] is None


def test_fresh_matching_snapshot_revalidates_invalidated_evidence_without_a_new_test():
    value = observation(point(), point(), unittest_result('Ran 2 tests in 0.001s\n\nOK\n'), 'pytest', 1)
    stale = invalidate(value, 'exec_command')

    current = point()
    current['code']['scope'] = 'bounded project files'
    current['observed_at'] = 123.0
    same_code = compare(stale, current)
    assert same_code['status'] == 'passed'
    assert same_code['applies_to_current_code'] is True
    assert same_code['historical'] is False
    assert same_code['test_execution_is_historical'] is True
    assert same_code['invalidated_by'] == 'exec_command'
    assert same_code['invalidated'] is True
    assert same_code['applicability_revalidated_after_invalidation'] is True
    assert same_code['applicability_check_source'] == 'workspace_snapshot_fresh_bounded_fingerprint'
    assert same_code['applicability_checked_at'] == 123.0
    assert same_code['applicability_check_scope'] == 'bounded project files'
    assert same_code['environment_verified_current'] is False

    changed_code = compare(stale, point('def'))
    assert changed_code['applies_to_current_code'] is False
    assert changed_code['historical'] is True
    assert changed_code['applicability_revalidated_after_invalidation'] is False

    changed_config = compare(stale, point(config='other-env'))
    assert changed_config['applies_to_current_code'] is False

    incomplete = compare(stale, point(complete=False))
    assert incomplete['applies_to_current_code'] is None
    assert incomplete['applicability_revalidated_after_invalidation'] is False

    missing_fingerprint = point()
    missing_fingerprint.pop('config_digest')
    assert compare(value, missing_fingerprint)['applies_to_current_code'] is None

    known_code_change_missing_config = point('def')
    known_code_change_missing_config.pop('config_digest')
    assert compare(stale, known_code_change_missing_config)['applies_to_current_code'] is False


def test_error_timeout_and_unknown_execution_never_claim_passed_evidence():
    for result in (
        {'exit_code': 0, 'execution_state': 'unknown'},
        {'exit_code': 0, 'execution_state': 'finished', 'timed_out': True},
        {'exit_code': 0, 'execution_state': 'finished', 'error': 'response lost'},
    ):
        value = observation(point(), point(), result, 'pytest', 1)
        assert value['test_result'] == 'unknown'
        assert value['status'] != 'passed'
        if value['process_exit_status'] == 'unknown':
            assert value['reliable_completion'] is False
            assert compare(value, point())['applies_to_current_code'] is None


def test_unconfirmed_cancel_or_timeout_cleanup_cannot_become_terminal_evidence():
    for result in (
        {
            'stdout': '', 'stderr': '', 'exit_code': 130,
            'execution_state': 'unknown', 'remote_task': {
                'reason': 'supervisor_signal', 'cleanup_state': 'unconfirmed',
            },
        },
        {
            'stdout': '', 'stderr': '', 'exit_code': 124, 'timed_out': True,
            'execution_state': 'unknown', 'remote_task': {
                'reason': 'timeout', 'cleanup_state': 'unconfirmed',
            },
        },
    ):
        value = observation(point(), point(), result, 'python -m unittest', 1)
        assert value['process_exit_status'] == 'unknown'
        assert value['test_result'] == 'unknown'
        assert value['reliable_completion'] is False


def test_framework_report_and_process_exit_are_separate_and_skips_stay_unknown():
    ok = 'Ran 3 tests in 0.001s\n\nOK\n'
    wrapper_failed = observation(point(), point(), unittest_result(ok, exit_code=1), 'pytest', 1)
    assert wrapper_failed['process_exit_status'] == 'failed'
    assert wrapper_failed['test_result'] == 'success'
    assert wrapper_failed['test_summary']['state'] == 'failed'
    assert wrapper_failed['test_summary']['report_status'] == 'success'
    assert wrapper_failed['status'] == 'failed'

    skipped = observation(
        point(), point(), unittest_result('Ran 3 tests in 0.001s\n\nOK (skipped=1)\n'),
        'pytest', 1)
    assert skipped['test_result'] == 'completed'
    assert skipped['reported_skips'] == 1
    assert skipped['executed'] is None and skipped['passed'] is None

    unrecognized = observation(point(), point(), unittest_result('finished with exit 0\n'), 'pytest', 1)
    assert unrecognized['process_exit_status'] == 'success'
    assert unrecognized['test_result'] == 'unknown'
    assert unrecognized['status'] == 'unknown'
    assert unrecognized['reliable_completion'] is True


def test_summary_is_detected_on_either_complete_stream_without_concatenating():
    stderr_result = {
        'stdout': 'diagnostic text without a report\n',
        'stderr': 'Ran 1 test in 0.001s\n\nOK\n',
        'exit_code': 0, 'execution_state': 'finished',
    }
    value = observation(point(), point(), stderr_result, 'python -m unittest', 1)
    assert value['test_result'] == 'success'
    assert value['summary_stream'] == 'stderr'

    ambiguous = {
        'stdout': 'Ran 1 test in 0.001s\n\nOK\n',
        'stderr': 'Ran 1 test in 0.001s\n\nOK\n',
        'exit_code': 0, 'execution_state': 'finished',
    }
    value = observation(point(), point(), ambiguous, 'python -m unittest', 1)
    assert value['test_result'] == 'unknown'
    assert value['test_result_reason'] == 'multiple_complete_unittest_summaries'

    incomplete = {
        'stdout': 'Ran 1 test in 0.001s\n\nOK\n',
        'exit_code': 0, 'execution_state': 'finished',
    }
    value = observation(point(), point(), incomplete, 'python -m unittest', 1)
    assert value['test_result'] == 'unknown'
    assert value['test_result_reason'] == 'complete_output_unavailable'
    assert value['reliable_completion'] is False
