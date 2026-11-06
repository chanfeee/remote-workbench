# Inline card history (0.4.5)

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

## Existing records

The previously shipped v2, v4, v5 and v6 resource URIs remain readable. Upgrading
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
