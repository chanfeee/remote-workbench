from remote_workbench import audit


async def test_explicit_byte_budget_returns_small_diff_beyond_legacy_line_limit(local):
    executor, _ = local
    command = "python3 -c 'for i in range(350): print(\"line\", i)'"
    explicit = await executor.run(command, output_budget=32768)
    assert explicit.exit_code == 0 and not explicit.truncated
    assert explicit.stdout.splitlines() == ["line " + str(i) for i in range(350)]
    legacy = await executor.run(command)
    assert legacy.exit_code == 0 and legacy.truncated
    assert "[truncated lines]" in legacy.stdout
    bounded = await executor.run(command, output_budget=256)
    assert bounded.truncated and len(bounded.stdout.encode()) <= 256
    assert (audit.directory(bounded.output_id) / "stdout").read_text() == explicit.stdout
