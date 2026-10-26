# Validation status

## Actual-directory-only development (0.3.0a15, 2026-10-26)

- Baseline: `develop` at `c7571b0`, preserving pre-existing uncommitted work. Session and fixed-workspace entry points use the selected actual directory. `workspace_switch(host, cwd)` no longer accepts `directory_mode`; copy preparation, copy delivery tools and copy-origin recovery exceptions have been removed.
- Old copy files, commits, logs and stored local history are not deleted or migrated. Unsupported saved bindings require selecting the actual directory. Old copy jobs require manual terminal takeover; direct-directory jobs retain immutable origins and restart/lost-reply recovery.
- Final integrated local regression: **677 passed in 184.31 seconds**. Obsolete copy-only cases were retired; shared-directory, conflict, background-history, registry and compact-output cases now exercise actual-directory behavior. Tests cover copy-interface rejection before SSH, preserved old files/records, independent code/environment/run/result paths, shared write coordination and original-job recovery.
- Offline wheel and source archive builds passed; all packaged runtime files match the final tested source, the deleted copy module is absent, and private paths/local reports are excluded. Embedded remote source passes Python 3.9 syntax parsing; documentation links and `git diff --check` pass.
- No real CERN/IHEP execution, App loaded-version acceptance, paid token benchmark, publication or CI run was performed for this change. The independent stable installation and global configuration remain unchanged. Reload all related development MCP processes before using the changed interface.
- Publication preparation updates only the package version, changelog and documentation after the 677-test run; CI validates the committed candidate before merge.
- Entries below retain historical validation facts; their copy/delivery behavior is superseded by this update.

## Linux CI transport follow-up (0.3.0a14 develop)

- The first published phase-1 CI run ([40260425221](https://github.com/chanfeee/remote-workbench/actions/runs/40260425221)) passed packaging but exposed Linux `E2BIG` failures: nested base64 worker/supervisor source exceeded the single-argument limit. Local macOS execution had not reproduced that limit. Python 3.10 also exposed a test that incorrectly required compact summarization of every unittest progress format.
- Large Python source is now compressed with stdlib zlib before base64 encoding. The representative full worker/supervisor command is 31,780 bytes, below the usual 128 KiB Linux single-argument limit. Request bytes and heartbeats continue to use the existing stdin protocol; native login shells, command framing and process supervision are unchanged.
- Four added transport regressions cover small/large Unicode source, binary stdin, the final nested command size and a real managed file roundtrip with an emulated Linux per-argument cap. The runtime fix passed a full **540-test local regression in 139.05 seconds**, with 122 frozen files unchanged. A subsequent test-only correction permits the existing raw-output fallback for older unittest skip continuations; the raw report, output ID and unknown execution counts remain checked, with **17 related checks passed**. No general parser expansion was introduced. The groups overlap and are not additive.
- These checks use local workers and simulated SSH. The corresponding pushed commit's Linux/Python matrix is a separate CI result; they do not certify actual CERN/IHEP behavior or client-loaded code. Existing main/stable installations, copy directories and unpublished report edits remain unchanged.

## Ordinary execution without automatic project-test management (0.3.0a14 develop)

- Phase 1 started from actual local `develop` commit `ae380a95c4bb479baf3d77980df334a882d09013`; fetched `origin/develop` was `6eb86fa8c69223ebc05dc4be94eba3ec3d7b1feb`. The three existing local commits and pre-existing report edits were preserved. This phase does not implement direct mode or change copy binding defaults.
- Default script, pytest and unittest execution, managed edits, job start/control/recovery and local resume do not capture project test fingerprints or read/write test-evidence caches. Fail-fast spies cover the retired registry/evidence interfaces; real local worker requests show **zero implicit fingerprints**. Raw command/output/audit links, SHA256 conflicts, patch checks and the existing timeout/cancellation protections remain covered.
- `workspace_resume(include_legacy_test_history=True)` explicitly reads old local observations without rewriting their rows or checking current code. `workspace_snapshot()` now returns metadata/Git facts without content scanning or history comparison; `scan_content=True` explicitly scans the bound project root with declared budgets. Acceptance observed exactly one explicit fingerprint with `max_files=13` and `max_content_bytes=4096`, and no changes to seeded legacy rows.
- Integrated local regression: **536 passed in 145.19 seconds**. All 121 frozen source/test/script and package-metadata files were unchanged during the run. A preceding 110-check targeted group and the two independent stdio acceptance cases also passed; these groups overlap and are not additive. Earlier fixture failures were corrected without bypassing production path/process identity checks; local worker `ps` and loopback tests require local permissions.
- The actual MCP stdio restart demonstration uses simulated SSH and a bounded local worker: drop the successful launch reply after persistent remote state exists, restart MCP, switch to B, recover the original A job, receive live partial output, and continue byte cursors without duplicated prefixes. One launch, one stable recovered output handle and A-only job controls were checked. The worker confirmed cancellation with exit code 130; the collector then published the same terminal state. `job_wait` progress is distinct from completion. Process-group cleanup remains best effort (`tree_confirmed=false`).
- Full/compact profiles still expose 30/8 tools. Existing normal operations retain their inputs; resume adds an opt-in legacy-history flag and snapshot adds the opt-in scan flag and budgets. Python 3.10 syntax checks passed for 55 local package modules, and Python 3.9 syntax checks for nine remote sources. These are syntax checks; the regression runtime was local Python 3.13.
- Candidate wheel/sdist builds and extracted-wheel smoke checks passed for `0.3.0a14`. All 59 packaged application/resource files matched the tested source; the wheel exposed both expected tool profiles and opt-in defaults. License files were preserved, and private configuration/runtime paths and the excluded historical report were absent. Builds use a clean public-file export so unrelated untracked files are not packaged.
- Historical a12/a13 evidence-management implementations and their recorded validation below remain historical facts. Existing copy contents, delivery guards, job origins and stored history are retained. Direct/shared-directory coordination, remaining C3 work, copy symlink/repeat-delivery fixes and viewer improvements remain later stages. No real CERN/IHEP SSH, actual Codex App loaded-version acceptance, paid model benchmark, remote-data migration, main/stable promotion or publication/CI run was performed in this phase. The independent stable remains `0.3.0a10`.

## Recoverable background test evidence (0.3.0a13 develop)

- Three behavior commits cover the D1 subTest boundary, C2 recovery of the original background job, and D3 persistent test attempts/collector-driven observations. Direct mode, resource coordination and default-directory changes remain later work.
- Final full local regression: **538 passed in 142.93 seconds**. The first full run exposed an authentication-refusal ordering regression; the narrow fix passed all 19 authentication checks before the final full rerun. The frozen Python/shell source hashes were unchanged during that successful run.
- Real MCP stdio subprocess restart with simulated SSH and a real local worker passed: deliberately drop the successful job-start reply after state persistence, restart the controller, bind B, reconnect the original A job, receive partial output before releasing its gate, and persist A's completion without job-status polling. One background launch, stable job/attempt/output identities, byte cursors without duplicate output, and a later foreground result surviving the older background completion were checked.
- Targeted local worker coverage also includes failed tests, confirmed cancellation, collection disconnect/unknown and reattachment of the same attempt, and an injected SQLite evidence-write failure followed by recovery. Complete stdout/stderr are bounded for summaries; process exit, framework result and code association are separate facts. Historical reads never refresh completed after fingerprints; explicit snapshots compare current code while dependencies/environments remain unverified.
- Public full/compact input schemas remain unchanged (30/8 tools). Local Python 3.10 and remote-source Python 3.9 syntax checks passed. These are syntax checks; the full run used local Python 3.13. No live IHEP/CERN SSH, actual Codex App loaded-version acceptance, new paid model benchmark, default direct activation or stable promotion was performed.
- Usage, guarantees and a repeatable local stdio demonstration: [background task recovery](docs/lifecycle-recovery.zh-CN.md). The independent local stable remains 0.3.0a10.

## First implementation batch (0.3.0a12 develop)

- Five bounded child-agent tasks delivered C1 log pagination, D1 skipped-test summaries, D2 evidence freshness, E1 an offline cost ledger, and the A1/A2 design contract. Parent review requested corrections before integration; direct/copy modes and cross-chat resource locks remain design-only.
- Integrated local regression: 515 tests passed (108.68 seconds), using local fixtures and simulated SSH. No real server or credential was accessed in this batch. Original cursor/skip probes no longer reproduce; the copy symlink and repeat-delivery probes still reproduce and remain F1/F2 work.
- Both real worker-usage files replay as 18 requests / 727,763 total tokens without double-counting cumulative usage, cache, or reasoning. Missing speed/rate/debit information remains unknown; supplied-record completeness does not certify full task or account costs.
- Wheel/sdist builds passed. The independent stable remains 0.3.0a10; no new paid model comparison, remote failure injection, default-directory change, or stable promotion was performed.

## Compact task workflow (0.3.0a11 develop candidate)

- Full local regression after the explicit-byte-budget fix: 477 tests passed (99.35 seconds). The later inclusive 2% gate passed its 10 targeted checks. Wheel/sdist builds passed; this develop candidate has not been promoted to main or the independent stable installation.
- Opt-in session profile: eight exposed tools; advanced operations retain their original validation and routing behind an on-demand gateway. The full profile remains the default.
- Deployment-specific SSH replays are private acceptance records and are not included in this portable validation document. Automated local coverage checks the same editing, output and diff interfaces with disposable fixtures.
- Successful test replies in this replay shrank from 1183 to 373 UTF-8 bytes after removing repeated normal metadata. Bytes are not tokens or subscription usage.
- The current complete-task ceiling is **2% extra**, inclusive, with original functionality preserved. A fresh actual nine-vs-nine pair on the loaded candidate used 355,677 native vs 372,086 remote tokens (+4.613%); both passed the same 21-case independent acceptance, but the usage gate remains **not qualified**. Original 1% historical reports are retained. The checker rejects different baseline/task hashes, models/settings, request counts, failed acceptance and incomplete/retry-excluded totals.
- Independent stable remains 0.3.0a10. A local project-only override selects the develop candidate without changing global MCP registration or other projects.

## Stable source promotion and GNU grep (0.3.0a10)

- Full local regression suite: 437 tests passed (95.82 seconds).
- GitHub Actions for the preceding develop commit exposed single-file GNU grep output without filenames on Python 3.10–3.13. The tool now requests `-H` explicitly; native-shell test assertions remain unchanged.
- `main` is the stable source branch. A fixed commit is promoted after develop CI succeeds; main CI and independent local wheel acceptance are checked before updating the local stable installation.

## Default viewer refresh (0.3.0a9)

- Targeted viewer/session regressions: 42 tests passed (7.05 seconds), including the shipped HTML checkbox default, polling without a click, manual off, page restore, and manual refresh after disabling polling.
- Full-suite results below belong to their recorded releases; the HTML default change does not alter command execution or image fetching.

## Chat image fetch (0.3.0a8)

- Full local regression suite: 435 tests passed (95.86 seconds).
- Actual develop MCP stdio acceptance passed for PNG, JPG, SVG and PDF page 2, using a local SSH fixture. Binary originals matched; generated previews opened successfully and the PDF page content was checked.
- Each MCP reply is one compact text result, measured at 1040–1068 UTF-8 bytes in this fixture, with local paths and chat Markdown rather than binary/base64 payloads. These are byte counts, not tokenizer measurements.
- Separate chat caches were verified. Missing, non-image, oversized and nonregular files, invalid pages, unavailable renderers, and external/active SVG references are covered. The remote worker still parses as Python 3.9; image rendering libraries run locally.
- No real server connection or Codex App chat-rendering acceptance was performed. The HTML viewer is unchanged. See [聊天内看图](docs/images.zh-CN.md).

## Historical paired token workflow sample (0.3.0a7) — 2026-10-25

- The fixed `0.3.0a7-0958f5b` build was compared with native local tools using fresh subagents, the same `gpt-6-luna` model and identical frozen CSV-project files. This does not characterize later stable builds.
- Actual execution-worker usage, including cached input: MCP 1,219,857 tokens / 22 model responses; native 304,203 / 8. The observed workflow difference was 915,654 tokens (4.0100× total usage), not a pure protocol-cost or billing measurement.
- Both arms passed 28 CSV-project tests, six independent acceptance checks and diff checks. These are fixture checks, not the Remote Workbench regression suite.
- The first remote run received coordinator follow-ups and is excluded from the primary pair. Guardian usage is recorded separately because billing treatment is unknown. The earlier approximately 2% figure concerned only identified reply redundancy, not complete-task net overhead.
- Individual run reports, per-response ledgers and review evidence remain local and are excluded from both Git and source distributions. Portable documentation contains no personal run paths, thread/response identifiers or temporary viewer URLs.

## Viewer instance isolation (0.3.0a7)

- Full local regression suite: 410 tests passed (92.89 seconds). Targeted viewer/session checks also passed: 40 tests (6.34 seconds).
- Coverage includes OS-assigned loopback ports, a foreign local HTML service holding the preferred port, simultaneous viewers, port reuse with stale URLs, instance response headers, workspace-scoped routes, and browser auto-refresh identity checks.
- Actual develop launcher acceptance over MCP stdio and loopback HTTP passed: two MCP processes, three local chats, separate audit records, valid instance links, and stale/cross-chat record rejection. SSH invocation count: zero.
- Tests use loopback HTTP and simulated SSH. Codex App integration and real IHEP/CERN SSH execution are separate acceptance steps; neither is implied by these results.
- Implementation and handoff state: [Develop 交接记录](docs/develop-handoff.zh-CN.md).

## Develop task lifecycle and compute review (0.3.0a5) — 2026-10-24

- Final local regression suite: 316 tests passed (121.95 seconds), using simulated SSH and bounded local processes.
- Coverage includes cancellation, controller disconnect/heartbeat expiry, foreground and detached deadlines, owned-group cleanup, identity mismatch refusal, job stopping after workspace switches, and compute-review refusal before remote execution. Six local shell variants remain covered.
- Wheel and source-distribution builds passed. Archive inspection confirmed that private runtime/configuration directories are absent. The extracted wheel imported the new modules, advertised the cleanup entry point, exposed 17 fixed-workspace and 20 session tools, and its remote worker source parsed with Python 3.9 syntax compatibility.
- No IHEP/CERN commands or resource-exhaustion reproductions were run for this version. Cluster acceptance is still pending. The independent stable installation remains 0.2.4.
- Process-group cleanup is best effort for processes which stay in that group. Compute approval is an agent-reported user-confirmation workflow, not an OS sandbox or independently verified chat authorization; see [task lifecycle boundaries](docs/process-safety.zh-CN.md).

## Current account-shell update (0.2.1) — 2026-10-23

- Full regression suite: 192 tests passed (33.87 seconds).
- Real local shell coverage: bash, sh, dash, ksh, zsh, and tcsh. Tests exercise login startup files, command and background exit codes, guarded file edits, Git, search, directory listing, binary stdin, framing, quoted paths, trace output, and refusal to execute after a directory change fails.
- Real remote recheck: 23 engineering tests and 11 workflow checks passed. The selected shell was compared with the server account's system login-shell entry; no account or global shell configuration was changed.
- A freshly installed 0.2.1 wheel passed all 10 entry-point/resource/MCP smoke checks. Both new helper modules are present in the wheel, and generated remote code was checked for Python 3.9 syntax compatibility.
- Commands and detached jobs share account-shell selection. Generated file/search/Git operations pass argument arrays instead of relying on the account shell's parsing rules. Limited search output is drained to preserve the real process exit code.
- The MCP channel has no PTY. Interactive login startup files are loaded, but terminal-only initialization and job control can differ from a manual SSH terminal.

## Initial independent distribution (0.2.0) — 2026-10-23

- Local regression suite: 154 tests passed in the independent `remote-workbench` checkout environment (16.17 seconds).
- MCP stdio initialization handshake passed.
- Wheel and source distribution builds passed.
- Independent environment installation with the locked dependencies passed. The renamed package imports from the independent checkout and reports version 0.2.0.
- The built wheel was installed in a separate empty environment with dependencies constrained by the lock file. Ten smoke checks passed, including all six command help entries, isolated package imports and bundled resources, and MCP stdio handshakes exposing 25 multi-host tools or 16 bound-workspace tools. The upstream `onirika` package was absent from that environment.
- Remote recheck through the renamed MCP passed: all 23 tests in the existing engineering project passed again. Nine workflow checks also passed, covering the default remote directory and the tested account's login shell, 16 bound tools, guarded edits, unified patch validation and application, execution of the modified code, Git and saved edit diffs, local audit metadata, and the local viewer.
- The edit checks used a newly created disposable remote test directory. The agent and MCP stayed local; no Codex CLI was installed or run remotely.

## Historical remote engineering exercise on the predecessor — 2026-10-23

A separate Python CSV statistics project was developed over SSH using the predecessor package before the rename. Its final 23 unit and acceptance tests passed, including a 50,000-row input case. The exercise covered an initial failing feature test, implementation, reviewable Git changes, and final checks. The renamed MCP reran the final 23 tests successfully, as recorded above; the entire development sequence was not repeated.

Earlier real-host MCP/SSH and App audit-viewer checks were also performed on the predecessor. Raw records, host names, personal paths, and screenshots are intentionally not included in this repository.

Every live test requires an explicit `--host`; some also require a parent or target directory. These tests execute commands and make changes on the selected remote host, so run them only against a disposable test area.

The local audit viewer was designed to show the actual commands, output, and diffs in a browser. It does not create native Codex terminal or file-change cards. The compact workspace MCP defaults (2 KiB for command output, 4 KiB for file/patch/diff previews) reduce tool-result payloads; they do not prevent token use when an agent reads full output through MCP.
