# Changelog


## 0.3.0a15 — actual directories and structured operations

- Bind actual remote directories without project copies; remove copy preparation, copy delivery and old-copy recovery. Existing files and records are not deleted. Old-copy jobs require terminal takeover.
- Keep chat records and immutable job origins separate from code; coordinate managed writes in known overlapping directories on one controller/shared state.
- Add snapshot-backed guarded edits, literal argv calls, sequential command batches and explicit switch/read and edit/check combinations.
- Preserve manual OpenSSH authentication, supervised cancellation, bounded output, local audit records and explicit-only project fingerprinting.
- Make the default README English and provide a Chinese version. Keep personal reports and runtime configuration out of source distributions.
- Validation: 677 local tests passed before publication preparation; CI and installed-client verification are separate checks. Independent stable installations are unchanged.

## 0.3.0a14 (develop)

- Compress large remote Python bootstrap source before base64 encoding so nested worker/supervisor commands stay below Linux's single-argument size limit. Preserve the existing stdin, login-shell and supervision protocols.
- Route scripts, test runners and builds through the same ordinary command/job lifecycle. Remove automatic project fingerprints, test attempts, cache invalidation and test-evidence audit/collector callbacks from execution and recovery.
- Make legacy test history an explicit, local-only `workspace_resume(include_legacy_test_history=True)` read. Preserve stored observations without asserting that they apply to current code.
- Make project content scanning opt-in through `workspace_snapshot(scan_content=True, max_files=..., max_content_bytes=...)`; default snapshots inspect metadata and Git state without content fingerprints or test-history comparison.
- Keep factual command/output/job records, original-target recovery, guarded edits and copy delivery protections. Preserve truncation in compact replies, and keep ambiguous unittest/subTest counts unknown.
- Align the development scope with generic execution and recovery. Direct mode and limited shared-directory coordination remain later work; existing copy targets, historical records and the independent stable installation retain their behavior.

## 0.3.0a13 (develop)

- Separate unittest's reported top-level tests and skips. Skipped subtests can make execution/pass counts unknowable; compact replies and HTML use the same facts and retain raw output access.
- Recover the original background job after a lost launch reply or MCP restart, using persisted target/endpoint/path identity. Reuse one local output handle and collector, continue byte cursors, and preserve unknown states instead of resubmitting commands.
- Register test attempts before execution and persist collector/recovery observations against the original project. Keep latest attempts, reliable completions and current-code association separate; order records by attempt start and preserve completed historical evidence on repeated reads.
- Keep process outcomes separate from recognized unittest results. Missing summaries, incomplete fingerprints and unverified dependencies/environments remain explicit; workspace resume stays local-only.
- Reject authentication commands before job origin/reservation and test-attempt writes, retaining only a redacted refusal audit.

## 0.3.0a12 (develop)

- Advance contextless saved-log search pages without repeating the same match; preserve overlapping matches and byte budgets.
- Preserve unittest skipped/executed counts and expected failures; avoid deriving passing-test counts from failed subtest summaries.
- Treat workspace recovery results as historical observations. Invalidate current test applicability on possible writes and re-associate matching code only through a fresh bounded snapshot, without claiming environment verification.
- Add an offline cost ledger with explicit rate snapshots, stable request deduplication, separate actual/estimated amounts, and visible unknown coverage. Keep it as a test helper for manually run, independent benchmark samples; it does not establish full-task savings or gate routine task completion.
- Clarify that the Remote Workbench path for routine commands, edits, and tests does not collect token/credit or model-fee data or add metering model calls; the integration workflow does not append usage assessments to replies or UI. This scopes the statement to Remote Workbench and makes no claim about Codex App or model-service account usage records. Retain operation records and necessary execution diagnostics.
- Document direct/copy migration and resource-coordination contracts; these modes and locks are not implemented by this release.

## 0.3.0a11 (develop)

- Add an opt-in compact session tool profile, on-demand advanced schemas, reusable workspace audit links and conservative successful-test summaries.
- Combine explicit workspace selection and its first inspection without another model round.
- Accept context patches without model-calculated hunk counts; keep standard unified diffs in the human audit viewer.
- Honor explicit command-output byte budgets without the legacy 200-line ceiling; preserve byte caps, complete local spools, and the default line cap when no byte budget is supplied.
- Add a complete-task token budget gate with the user's maximum 2% extra usage; the latest valid nine-vs-nine sample is +4.613% and does not qualify.

## 0.3.0a10

- Always include filenames in remote grep matches, including a single input file on GNU grep; preserve the same result format across Linux and macOS.

## 0.3.0a9 (develop)

- Enable automatic refresh by default when opening the local audit viewer; keep the manual off switch and page-restore behavior.

## 0.3.0a8 (develop)

- Add `fetch_image` for private local copies of remote PNG/JPG/PDF/SVG files, returning chat image Markdown and paths without binary MCP payloads.
- Render selected PDF pages and self-contained SVG plots to local PNG previews through the optional `images` extra; retain original files on preview failure.
- Keep image caches scoped to each chat. The HTML viewer is unchanged.

## 0.3.0a7 (develop)

- Assign each local audit viewer an OS-selected loopback port by default and a random per-process URL prefix, so other project pages are not reused or overwritten.
- Keep operation links pointed at the actual viewer instance; reject stale URLs and prevent auto-refresh from rendering responses from another instance.
- Preserve per-session audit data and workspace routing under the instance-prefixed viewer URL.

## 0.3.0a5 (develop) — 2026-10-24

- Supervise foreground commands and detached jobs with owned process groups, remote deadlines, foreground heartbeats, and TERM-to-KILL cleanup.
- Add `stop_job` and the independent `remote-workbench-cleanup` entry point, with node and process-identity checks and original-target routing.
- Require explicit user confirmation of substantial compute plans through the agent workflow; ordinary operations remain direct, without cgroup requirements or an account-wide process cap.
- Select public source-distribution files explicitly so local configuration, virtual environments, audit records and runtime data stay out of packages.
- Run CI for pushes to develop as well as main. This development release does not update the independent stable installation.

## 0.2.1 — 2026-10-23

- Use the remote account's configured login shell for commands and detached jobs, with no zsh fallback or fixed-zsh live-test requirement.
- Launch login shells using the SSH-style login name and preserve native startup files, byte-safe stdin, output framing, project directory binding, and shell-specific tracing.
- Report the selected shell in `workspace_info` and document the difference between a tool command channel and a manual SSH terminal.
- Run generated file/search/Git operations with argument arrays, independently of the login shell's quoting and pipeline syntax; preserve real search exit codes while limiting returned lines.

## 0.2.0 — 2026-10-23

This version prepares an independent Remote Workbench distribution from the Onirika 0.1.0 source baseline. See [UPSTREAM.md](UPSTREAM.md) for the exact upstream commit.

- Added local operation records for remote commands, inputs, outputs, and file diffs, with a read-only local viewer.
- Added bounded output previews and paged retrieval to reduce the amount of data returned through MCP.
- Added guarded file edits and checked unified Git patch application on the remote host.
- Added `remote-workbench-workspace`, which binds a local MCP process to one remote host and project directory.
- Renamed the Python namespace, configuration directory, environment variables, and command entry points for the independent project.

The renamed source passed 154 local regression tests, package builds, installation checks, and a remote recheck. Details and the earlier development exercise are documented in [VALIDATION.md](VALIDATION.md).
