"""Legacy offline helpers for reading or exercising historical test evidence.

The normal session execution and background collection paths do not import or
call this module. A file digest does not certify installed dependencies or an
environment activated inside an arbitrary shell command.
"""
from __future__ import annotations

import hashlib
import json
import time

from remote_workbench.baseline import project_fingerprint

SUMMARY_OUTPUT_LIMIT = 1024 * 1024


def config_fingerprint(snapshot):
    return config_fingerprint_target(snapshot.workspace.executor, snapshot.host,
                                     snapshot.cwd, snapshot.project_root)


def config_fingerprint_target(executor, host, cwd, project_root):
    config = executor.config
    selected = {name: getattr(config, name, None) for name in (
        'ssh_host', 'user', 'port', 'preamble', 'command_timeout', 'job_timeout',
        'terminate_grace_seconds')}
    selected.update(cwd=cwd, project_root=project_root, host=host)
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


async def capture(snapshot):
    try:
        content = await project_fingerprint(snapshot.workspace.executor, snapshot.project_root)
    except Exception as exc:
        content = {'complete': False, 'digest': None, 'reason': type(exc).__name__}
    return {'code': content, 'config_digest': config_fingerprint(snapshot), 'observed_at': time.time()}


async def capture_target(executor, host, cwd, project_root):
    try:
        content = await project_fingerprint(executor, project_root)
    except Exception as exc:
        content = {'complete': False, 'digest': None, 'reason': type(exc).__name__}
    return {'code': content,
            'config_digest': config_fingerprint_target(executor, host, cwd, project_root),
            'observed_at': time.time()}


def read_complete_output(output_id, stream='stdout', fallback=None, *, truncated=False):
    """Read only a complete, bounded UTF-8 output spool for the D1 parser."""
    if output_id:
        from remote_workbench import audit
        try:
            if stream not in {'stdout', 'stderr'}:
                return None, False, 'invalid_output_stream'
            path = audit.directory(output_id) / stream
            if path.stat().st_size > SUMMARY_OUTPUT_LIMIT:
                return None, False, 'output_exceeds_1MiB_summary_limit'
            data = path.read_bytes()
        except (OSError, ValueError):
            return None, False, 'complete_output_unavailable'
        try:
            return data.decode('utf-8'), True, None
        except UnicodeDecodeError:
            return None, False, 'complete_output_is_not_utf8'
    if not isinstance(fallback, str):
        return None, False, 'complete_output_unavailable'
    if truncated:
        return None, False, 'output_preview_truncated'
    if len(fallback.encode('utf-8')) > SUMMARY_OUTPUT_LIMIT:
        return None, False, 'output_exceeds_1MiB_summary_limit'
    return fallback, True, None


def complete_output_evidence(result):
    """Check output availability independently from the parser's size limit."""
    output_id = result.get("output_id")
    if output_id:
        from remote_workbench import audit
        try:
            root = audit.directory(output_id)
            return all(not (root / stream).is_symlink() and (root / stream).is_file()
                       for stream in ("stdout", "stderr"))
        except (OSError, ValueError):
            return False
    return (not result.get("truncated") and
            isinstance(result.get("stdout"), str) and
            isinstance(result.get("stderr"), str))


def process_exit_status(result):
    state = str(result.get('execution_state') or '').lower()
    remote_task = result.get('remote_task')
    reason = remote_task.get('reason') if isinstance(remote_task, dict) else None
    cleanup = remote_task.get('cleanup_state') if isinstance(remote_task, dict) else None
    # A cause such as timeout/signal is not itself a terminal-state proof. If
    # the worker reports unknown cleanup/process state, keep the attempt
    # unknown even when an exit-like code or reason was observed.
    if (result.get('error') or cleanup == 'unconfirmed' or
            state in {'unknown', 'remote_state_unknown', 'cancellation_requested'}):
        return 'unknown'
    if result.get('timed_out') or state in {'timed_out', 'timeout'} or reason == 'timeout':
        return 'timed_out'
    if state in {'cancelled', 'canceled'} or reason in {'cancelled', 'supervisor_signal'}:
        return 'cancelled'
    if reason in {'controller_disconnected', 'controller_lease_expired'}:
        return 'unknown'
    code = result.get('exit_code')
    if state == 'finished' and isinstance(code, int) and not isinstance(code, bool):
        return 'success' if code == 0 else 'failed'
    return 'unknown'


def summarize_output(result, text, *, complete, stream='stdout'):
    from remote_workbench.presentation import summarize_output as parse_unittest_summary

    if not isinstance(text, str):
        return None
    record = dict(result)
    if record.get('execution_state') == 'finished':
        record.setdefault('status', 'finished')
    return parse_unittest_summary(text, record, stream, complete=complete)


def framework_test_result(summary):
    """Derive only the unittest report fact, separate from process status."""
    if not isinstance(summary, dict):
        return 'unknown'
    report_status = summary.get('report_status')
    if report_status == 'failed':
        return 'failed'
    if report_status != 'success':
        return 'unknown'
    if (summary.get('reported_skips') or summary.get('expected_failures') or
            summary.get('executed') is None or summary.get('passed') is None):
        return 'completed'
    if isinstance(summary.get('tests_run'), int) and summary['tests_run'] > 0:
        return 'success'
    return 'unknown'


def summarize_complete_streams(result, output_id, *, stdout_fallback=None,
                                stderr_fallback=None, truncated=False):
    if output_id:
        from remote_workbench import audit
        try:
            root = audit.directory(output_id)
            total = sum((root / stream).stat().st_size for stream in ('stdout', 'stderr')
                        if (root / stream).exists())
        except (OSError, ValueError):
            total = SUMMARY_OUTPUT_LIMIT + 1
        if total > SUMMARY_OUTPUT_LIMIT:
            return None, None, 'output_exceeds_1MiB_summary_limit'
    else:
        byte_count = sum(len(value.encode('utf-8')) for value in
                         (stdout_fallback, stderr_fallback) if isinstance(value, str))
        if byte_count > SUMMARY_OUTPUT_LIMIT:
            return None, None, 'output_exceeds_1MiB_summary_limit'
    found = []
    reasons = []
    all_streams_complete = True
    for stream, fallback in (("stdout", stdout_fallback), ("stderr", stderr_fallback)):
        text, complete, reason = read_complete_output(
            output_id, stream, fallback, truncated=truncated)
        if reason:
            reasons.append(reason)
        if not complete:
            all_streams_complete = False
        if complete:
            summary = summarize_output(result, text, complete=True, stream=stream)
            if summary is not None:
                found.append((summary, stream))
    if not all_streams_complete:
        return None, None, (reasons[0] if reasons else 'complete_output_unavailable')
    if len(found) == 1:
        return found[0][0], found[0][1], None
    if len(found) > 1:
        return None, None, 'multiple_complete_unittest_summaries'
    return None, None, (reasons[0] if reasons else 'unrecognized_unittest_summary')


def observation(before, after, result, command, revision, *, attempt=None,
                stdout=None, complete=True, output_id=None, summary=None,
                summary_stream=None, summary_reason=None, output_complete=None):
    if output_id and not result.get("output_id"):
        result = dict(result, output_id=output_id)
    if summary is None and stdout is not None:
        summary = summarize_output(result, stdout, complete=complete, stream=summary_stream or 'stdout')
    elif summary is None and stdout is None:
        summary, summary_stream, summary_reason = summarize_complete_streams(
            result, result.get('output_id'), stdout_fallback=result.get('stdout'),
            stderr_fallback=result.get('stderr'), truncated=bool(result.get('truncated')))
    process_status = process_exit_status(result)
    test_result = framework_test_result(summary)
    # The process result and the framework report are separate facts. An exit
    # code by itself never proves that tests ran or passed.
    if process_status in {'cancelled', 'timed_out'}:
        status = process_status
    elif process_status == 'failed':
        status = 'failed'
    elif process_status == 'success':
        status = {'success': 'passed', 'failed': 'failed', 'completed': 'completed'}.get(
            test_result, 'unknown')
    else:
        status = 'unknown'
    if output_complete is None:
        output_complete = complete_output_evidence(result)
    reliable = (output_complete and
                process_status in {'success', 'failed', 'cancelled', 'timed_out'} and
                result.get('execution_state') in
                {'finished', 'cancelled', 'canceled', 'timed_out', 'timeout'})
    stable = matches(before, after)
    applicable = True if reliable and stable else None
    value = {
        'operation_id': result.get('operation_id'), 'output_id': output_id or result.get('output_id'),
        'job_id': result.get('job_id'), 'exit_code': result.get('exit_code'),
        'process_exit_status': process_status, 'test_result': test_result,
        'test_summary': summary,
        'summary_stream': summary_stream,
        'test_result_reason': summary_reason or (None if summary else 'unrecognized_unittest_summary'),
        'tests_run': summary.get('tests_run') if summary else None,
        'reported_skips': summary.get('reported_skips') if summary else None,
        'executed': summary.get('executed') if summary else None,
        'passed': summary.get('passed') if summary else None,
        'command': command, 'revision': revision, 'status': status,
        'observed_at': time.time(), 'evidence': ('confirmed_process_terminal_and_bounded_content_fingerprints'
                                                  if reliable else 'process_state_and_bounded_content_fingerprints'),
        'before': before, 'after': after, 'code_unchanged_during_test': stable,
        'applies_to_current_code': applicable, 'reliable_completion': reliable,
        'environment_evidence': 'command_and_host_configuration_only',
        'environment_limit': 'Activated shell environment, installed dependencies and external inputs were not fingerprinted.',
    }
    if attempt:
        for key in ('attempt_id', 'attempt_order', 'job_id', 'started_at', 'cwd', 'target'):
            if key in attempt:
                value[key] = attempt[key]
    return value


def invalidate(last_test, operation):
    """Keep the result as history while withdrawing current-code applicability."""
    if last_test is None:
        return None
    value = dict(last_test)
    value.update(
        applies_to_current_code=None,
        historical=True,
        invalidated=True,
        invalidated_by=operation,
        invalidated_at=time.time(),
    )
    return value


def matches(left, right):
    return _same_known_state(left, right) is True


def _same_known_state(left, right):
    a, b = left.get('code', {}), right.get('code', {})
    if (not a.get('complete') or not b.get('complete') or not a.get('digest')
            or not b.get('digest')):
        return None
    if a['digest'] != b['digest']:
        return False
    if not left.get('config_digest') or not right.get('config_digest'):
        return None
    return left['config_digest'] == right['config_digest']


def compare(last_test, current):
    if last_test is None:
        return None
    value = dict(last_test, checked_at=time.time(), applies_to_current_code=None, historical=True)
    current_code = current.get('code', {})
    value.update(
        test_execution_is_historical=True,
        current_code_checked=True,
        applicability_check_source='workspace_snapshot_fresh_bounded_fingerprint',
        applicability_checked_at=current.get('observed_at') or time.time(),
        applicability_check_scope=current_code.get('scope'),
        applicability_check_configuration='workspace_execution_config_fingerprint',
    )
    if last_test.get('invalidated'):
        value['applicability_revalidated_after_invalidation'] = False
    before, after = last_test.get('before'), last_test.get('after')
    if after and after.get('config_digest') and current.get('config_digest'):
        value['applicability_configuration_matched'] = (
            after['config_digest'] == current['config_digest'])
    else:
        value['applicability_configuration_matched'] = None
    test_result = last_test.get('test_result')
    if 'test_result' in last_test:
        reliable_test = last_test.get('reliable_completion') is True
    else:
        # Historical rows predate explicit report summaries.
        reliable_test = last_test.get('status') in {'passed', 'failed'}
    if reliable_test and before and after and matches(before, after):
        if current_code.get('complete'):
            current_matches = _same_known_state(after, current)
            if last_test.get('invalidated'):
                value['applies_to_current_code'] = current_matches
                value['applicability_revalidated_after_invalidation'] = current_matches is True
            else:
                value['applies_to_current_code'] = current_matches
            value['historical'] = value['applies_to_current_code'] is not True
    value['environment_verified_current'] = False
    return value
