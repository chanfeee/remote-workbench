# Batch cards and historical results

## 0.4.12 batch execution with inline results

`exec_batch` returns the compact card with the original command result. The
first call can include `host` and an absolute `cwd` to bind and run together.
`review_activity` remains available for independently saved commands.

## 0.4.11 compact activity reviews

Following LiteRemote's activity review pattern, `exec_command` returns its
ordinary result and `operation_id`. At a group boundary, `review_activity`
reads the selected command records in order and renders one collapsed inline
card. This includes sequential calls that could not be batched and groups
with only one command. The overview shares a bounded output budget across the
group. Expanding it exposes the original command rows, and individual outputs
draw on demand. A review uses the current chat's saved records without SSH.

## 0.4.10 cards for command groups

`exec_batch` accepts two or more commands and binds the structured inline
card. Prefer groups of up to eight ready commands in one directory. Single
`exec_command` calls use the client's normal tool record, and `apply_patch`
binds the inline diff view. Saved cards remain readable under their original
resource URIs.

## 0.4.9 command and diff cards

`exec_command` and `apply_patch` use the original structured card layout.
An argv batch displays numbered commands and their outcomes in one card with
one shared output preview. The existing paging and deferred drawing paths
handle expanded output and diffs. Saved cards keep their own identities.

## 0.4.8 batch cards

Use `exec_batch` for ready command groups, preferably up to eight steps in
one directory. The tool binds one inline resource per batch and uses the same
executor and text result as `exec_command`. The card reads that result once,
draws output and exit codes on expansion, and uses a unique identity for each
saved batch. `exec_command` and `apply_patch` continue to return native text.

## Native text in 0.4.6 and 0.4.7

Command and managed-patch results in those versions have no `ui.resourceUri` or
`openai/outputTemplate` binding and no per-operation widget metadata. They use
ordinary MCP text and the client's native tool history, so a new operation
requires **zero custom HTML components**. The model-visible text and the eight
tool schemas retain their shapes; complete outputs/diffs remain in local
records, available through `read_output` and the records CLI.

To reduce native tool entries, the command description instructs callers to
submit already-known commands for the same directory as one
`command={commands:[argv,...]}` batch. As of 0.4.7, prefer up to eight commands
and send smaller ready batches immediately. All steps share one tool result
and output record while retaining their individual exit codes. Known post-edit checks
belong in `apply_patch.command`. This is a calling convention, not server-side
coalescing of separate requests: the client still owns the presentation of
independent tool calls. Calls that need earlier output to determine their next
arguments stay separate.

Resources from older releases remain readable for saved cards, with automatic
inline display disabled. They are compatibility views opened by the client on
demand, not the default presentation for new operations. Upgrading does not
remove existing records or re-execute commands. Previously cached or mounted
pages may remain until the host releases/reloads them.

## Background: released 0.4.5

Command and managed-patch cards represent individual operations. They are not a
single activity widget whose next result replaces its previous contents.

The desktop host coalesces results with the same resource and widget identity.
Resource v7 therefore gives each operation a stable, independent UI identity in
hidden result metadata. Reopening a result preserves that identity; it does not
execute the command again. The identity is a display hint, not a credential or
an output-access capability. Output reads still use the requesting chat's audit
scope.

The compact command/file rows request inline display when revisiting a chat.
Output and diff details remain collapsed until opened. The component accepts
live MCP notifications, initial tool metadata, canonical result envelopes and
delayed `openai:set_globals` hydration. Duplicate delivery of the same snapshot
does not discard the current expanded or paged view.

Model-visible result text and the eight-tool catalog are unchanged. Restoring
the saved preview needs neither SSH nor a model request. Only explicitly
requesting another output page calls the existing local `read_output` tool.
These desktop-specific display hints may be ignored by other clients.

## Historical-viewer performance (resource v8, 0.4.6)

For older cards opened by the client, the compatibility component postpones offscreen drawing, serializes only the latest pending
snapshot when visible, and pauses layout observation while offscreen or the
document is hidden. Visible updates and size notifications are coalesced per
animation frame; unchanged viewport width does not cause another layout read.
The protocol handshake remains immediate, including when a host initially hides
the iframe until connection completes. See the [MCP Apps lifecycle](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx#lifecycle)
and [Intersection Observer specification](https://w3c.github.io/IntersectionObserver/).

Command output and colored diff lines are created on expansion and released on
collapse. Paging updates a private copy of preview data, so collapsing/reopening
or receiving the same host metadata retains the loaded page without another
output read. Saved card identities and compact summaries are retained; no operation or output record is deleted. New text results do not allocate widget identities.

A generated host with 24 opaque sandboxed iframes, hiding each until initialized,
was compared in a fresh headless Chrome. With four cards initially visible:

| Measured work | v7 | v8 candidate |
| --- | ---: | ---: |
| Initial DOM nodes across all cards | 3,204 | 420 |
| Initial colored diff-line nodes | 2,448 | 0 |
| Initial output/diff preview elements | 24 | 0 |
| Layout reads during 80 repeated event rounds | 1,920 | 0 |
| Snapshot serializations during those rounds | 1,920 | 4 |
| Protocol initializations | 24 | 24 |

The browser check also opened diffs, closed a command while its output page was
in flight, reopened it with that page retained, repeated metadata delivery, and
scrolled to the last previously deferred card. All passed. Wide/narrow dark
layout checks retained 94 → 450 → 94 px command sizing and 94 → 225 → 94 px diff
sizing without horizontal overflow.

`node tests/inline_app_performance.cjs` is the dependency-free regression for
bounded background/repeated work; its observer model is separate from the real
browser comparison. These results measure component work, not actual Codex
typing latency or total memory. The host still owns iframe creation and
virtualization. The native-text candidate needs installation/reload and actual App acceptance before claiming that the reported whole-App slowdown is resolved. The table above measures only optional historical views; new operations do not create them.

## Existing records

The previously shipped v2, v4, v5, v6 and v7 resource URIs remain readable. Upgrading
does not rewrite earlier chat messages or add missing identities to old saved
results. Consequently, an old card already coalesced by the App may remain a
tool entry in that chat. The original local operation/output records remain
readable with `audit_get`, `read_output` or the records CLI described in the
README. Re-executing an operation is not a recovery method.

## Validation

- The 13 targeted inline tests passed in 7.80 seconds against the working tree
  and in 8.56 seconds against the frozen UI-only candidate. They cover independent
  command/patch identities, stable identities across serialization and local
  reconstruction, unchanged model content, chat isolation, bounded previews,
  command/diff rendering, pagination, delayed history hydration, status facts
  and all previously shipped resource URIs.
- A private replay through the installed desktop's actual history-coalescing
  function reproduced two hidden cards among three saved operations. Applying
  independent identities retained all three. Its resource parser also accepted
  compact inline display with a zero minimum height. No remote command ran.
- An isolated candidate is based on the installed 0.4.4 package with only
  `inline_app.py` and `operation_card.html` replaced. Its real local MCP stdio
  preflight passed with eight tools, v7 and all four historical resource URIs,
  starting in local mode. At that candidate-only step, the daily stable
  installation was unchanged.
- The temporary project MCP override was removed after it pinned
  `enabled = true`. Its replacement overrides only the candidate `command`;
  enable/disable, arguments, environment and approval policy inherit App
  settings. The bundled CLI's effective-config reader confirmed the candidate
  command and inherited settings. Stable rollout subsequently removed the
  project override completely; no global switch was changed.
- The complete 0.4.5 integration, including the runtime simplification, passed
  869 local tests in 215.65 seconds with no skips. Wheel/sdist construction and
  package-boundary checks passed. Stable commit `714ebec` also passed the four
  Python 3.10–3.13 CI jobs and the package build.
- Actual App follow-up confirmed the loaded stable 0.4.5 package and two
  successful short commands through an existing manual SSH connection. Both
  persisted results contained different operation-based widget identities.
  The user navigated to another chat and returned, then confirmed both command
  cards remained visible. The chat was restored to local mode. Full App restart
  and UI pagination were not exercised, and no new agent token benchmark is
  claimed. Private deployment details remain outside the published sources.
- A separate managed-edit check produced two full diff cards with different
  identities, restored the small fixture and removed its temporary directory.
  The user switched chats and returned, then confirmed both diff cards remained
  and both line diffs could be expanded. No runtime change was required.
