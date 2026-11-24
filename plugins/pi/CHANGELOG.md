# Changelog

All notable changes to the Pi extension are documented here. The version moves
in lockstep with the rest of the repo (`scripts/bump-version.sh`); releases are
tagged `vX.Y.Z`. The extension runs the matching mcp-combiner release, so each
entry also summarises the combiner's changes; the full list is in
[the combiner's changelog](https://github.com/georgeharker/mcp-combiner/blob/main/combiner/CHANGELOG.md).

## [0.26.0] - 2026-11-24

### Added

- **`/mcp-combiner connect`** connects now instead of waiting out the retry
  backoff (up to 10 s between background attempts), with the full cold-start
  budget. For a combiner the extension manages, it first attaches to it, or
  launches it if it isn't running, and lifts a respawn stop. It reports the
  tool count, or why it failed; background retrying carries on either way.
- **`/mcp-combiner collections`** lists each server collection: on or off for
  this chat and for the combiner, and its servers.
- **`/mcp-combiner collection on|off|reset <name>`** turns one on or off for
  this chat only, or back to the combiner's setting. Turning one on starts
  its servers for this chat if they weren't running.

The combiner renamed server profiles to *collections* in this release, and a
config that still says `profiles` no longer loads. See the combiner's
changelog.

## [0.25.0] - 2026-11-22

No extension changes. The combiner now carries every kind of server question
(a form, a URL to open, sampling, roots) between protocol revisions, on tool
calls, resource reads and prompts. A URL you agreed to open isn't asked again
while the browser step runs. It also fixes a consent-prompt loop between 2026
clients and 2026 servers. See the combiner's changelog.

## [0.24.5] - 2026-11-21

### Fixed

- **No more typing freeze after startup.** pi rebuilds its whole tool registry
  on every `registerTool`, and with codemode on that re-renders every tool's
  declaration, so registering the combiner's ~1,000 tools in one go froze the
  editor for 10 seconds or more. Tools (and removals of tools) now register in
  batches of about 50 ms each, yielding to pi between them. A time budget
  rather than a fixed count keeps the batches even, since each call gets
  slower as the registry grows. Typing lag during registration is now about
  55 ms (worst seen: 156 ms); all the tools arrive over about 10 s.
- A newer catalog (a `tools/list_changed` mid-registration) supersedes the
  pass in progress at its next batch, and `/new`, `/resume`, `/fork` or
  `/reload` stops it before pi retires the session's extension API.
- **Cold start no longer leaves the session without tools for 30 seconds.** The
  first connect used to start before the combiner was attached, and
  `sharedserver use` returns once the combiner is launched, not once it
  listens. A refused connect then failed for good until the 30-second footer
  refresh happened to reconnect. Connects now wait for the attach, and a
  refused first connect is retried (100 ms backoff up to 1 s, for up to 15 s).
  Once connected, a refused reconnect (a combiner restart) is retried for up
  to 3 s. Between retries pi checks the combiner process itself (see below:
  it respawns a dead one, or attaches to a new one). In a cold start test, the
  tools arrived at 3.8 s instead of 31.5 s.
- **A prompt sent while tools are still arriving waits for them.** A turn that
  started mid-registration, or while a combiner we just launched was still
  coming up, saw only part of the catalog. Now the turn waits (up to 30 s)
  until registration finishes. Waits longer than 250 ms show a notice (unless
  `PI_MCP_COMBINER_NOTIFY=false`). The registration keeps yielding while it
  waits, so the UI stays live. Esc, Ctrl+D or a double Ctrl+C end the wait at
  once; `/quit` and signals wait for it to finish (pi gives extensions no abort
  at this point). A combiner that is down and not ours to launch doesn't hold
  up the turn.
- **The combiner process is managed in one place** (`combiner-process.ts`):
  one attach function (`sharedserver use`) is the only way pi launches the
  combiner or takes its reference. Every pi process holds its own reference.
  - `use` runs once per pi process, unless the combiner changes. It used to run
    again after every connect and every new session, re-running the combiner
    version probes each time.
  - When the combiner becomes a different process (someone else restarted
    it, which drops every client's reference), pi attaches to the new one with
    the same function, so it keeps its reference.
  - When the combiner dies, pi notices within about 2 s (it watches the
    process it holds a reference on), reconnects, and respawns it with the
    same function. Several pi processes respawning at once is fine: sharedserver
    launches one and the rest attach to it. Respawning is capped at 2 a minute:
    a third death inside the minute stops respawning and reports it, with the
    combiner's log files. A new pi session lifts the stop.
  - If `sharedserver info` gives no answer (it normally answers in a few ms,
    so that's a sharedserver problem and pi says so once), pi judges the
    combiner by its own process check: gone means dead. pi never respawns a
    combiner it never managed to attach to.
- **The connection keeps itself up**, for managed and host-owned combiners
  alike: after a drop it reconnects on its own (500 ms, backing off to 30 s if
  connections keep dropping right after connecting), and after a failed
  connect it keeps retrying in the background (every 1 s, backing off to
  10 s) instead of waiting for the next request. A combiner someone else
  starts is picked up within seconds. A connect failure is reported once per
  outage, and recovery is logged.
  - `use` is retried when sharedserver reports the combiner as shutting down
    ("defunct ... Retry shortly"), and never after pi has started quitting.
- The footer could keep showing a stale `registering tools N/M` when an
  earlier refresh finished after a later one.

### Changed

- **Requires sharedserver 0.9.6 or newer** (the pi, OpenCode and Claude plugins
  alike; was 0.6.7 / 0.6.6). 0.9.6 adds a launch lock: several clients calling
  `use` on a stopped combiner at once now start exactly one and keep every
  reference (0.9.5 could start two and lose references), which the automatic
  respawn relies on. An older sharedserver found on your PATH makes the plugins
  fetch the current release, unless you set `SHAREDSERVER_BIN` explicitly.
- The footer shows `registering tools N/M` while a registration pass runs.
- Depends on `@earendil-works/pi-mcp` `^1.6.0` (was `^1.2.0`).

## [0.24.4] - 2026-11-20

### Fixed

- **pi exits promptly.** Every connect left an 8-second connect-timeout timer
  running (it was never stored, so it was never cleared), which held pi's
  process open after shutdown. The timer is now cleared and `unref`'d.
- **Faster startup.** With the combiner's faster `--version` (combiner
  changelog), the version check pi runs while starting takes about 0.1 s
  instead of 1.2 s, so startup drops from about 1.2 s to about 0.2 s when
  the combiner is installed.

## [0.24.3] - 2026-11-20 (not published; included in 0.24.4)

### Fixed

- Compatible with pi 1.6.0's TUI contract: the extension's components
  declare `render()` as returning `readonly string[]`, so they typecheck
  against `@earendil-works/pi-tui` 1.6, whose `Container.render` returns a
  cached read-only array. No behaviour change. The `pi-tui` peer range now
  includes the 1.x releases (`^0.85.1 || ^1.0.0`).
- `npm run smoke` builds first: it tests the built `dist/`, and a stale build
  had produced false failures. Its token-filter check now treats
  `open_widget` as a combiner tool, which no server filter hides.

## [0.24.2] - 2026-11-15

No extension changes. The combiner it runs now requires fastmcp 4.2.0.

## [0.24.1] - 2026-11-15

No extension changes. The combiner's CLI now explains clearly when it can't
reach a running combiner, and how to start or point at one.

## [0.24.0] - 2026-11-15

No extension changes. The combiner it runs:

- **Server profiles**: named groups of servers (`"profiles"` in the config).
  A profile loads only where its `when` matches (platform, host name,
  environment), so one config works across machines; a server runs if any
  of its profiles is on. `combiner__enable_profile` / `combiner__disable_profile`
  switch them; `MCP_COMBINER_PROFILES=+a,-b` at startup.
- `mcp-combiner authorize <server> [--open]` starts OAuth from the shell and
  prints the link.

## [0.23.1] - 2026-11-09

### Changed

- The package now ships this changelog, so Pi shows what changed after an
  update.

## [0.23.0] - 2026-11-09

No extension changes. The combiner it runs:

- **Isolated servers** (`isolate: true`, e.g. cribsheet, svg-mcp) list their
  tools through one shared connection; a chat's own session is only for its
  calls. A chat whose session died gets a fresh one on its next call, and is
  told once that its state on that server was reset.
- **Busy servers are never killed.** The combiner restarts a server by itself
  only when it's gone; one that's running but not answering prompts "Restart
  it?" on the next call to it.
- **Per-chat hidden tools**: `combiner__session_hide_tools` /
  `combiner__session_show_tools` hide tools by name or glob for one chat.
- A server that can't be listed is reported as failed, not as having no tools.
- `/status` page; web login checks the `Host` header; Tailscale docs.

## [0.22.0] - 2026-11-08

No extension changes. The combiner it runs:

- **Web login (`web_auth`)** for widget pages, the widget relay and the OAuth
  authorization page: a password (e.g. from 1Password via
  `MCP_COMBINER_WEB_PASSWORD`), single-use ticket links for MCP clients, this
  machine exempt, rate-limited sign-in.
- `combiner__authorize_server`, proper OAuth authorization pages, and a
  per-server `login_hint`.
- Resource changes from server lifecycle are announced.
- `/neovim/*` requires the inbound bearer when one is set.

## [0.21.3] - 2026-11-04

### Fixed

- When the combiner is owned by another host (Neovim, via
  `MCP_COMPANION_COMBINER_URL`) or not managed by pi (`PI_MCP_COMBINER_MANAGE=false`),
  a connect no longer calls `attachCombiner`, which isn't declared on that path:
  the call threw a `ReferenceError` (temporal dead zone) on every reconnect.

## [0.21.2] - 2026-11-03

### Changed

- Package metadata (`repository`, `bugs`, `homepage`, preview image) points at the
  renamed repository, `georgeharker/mcp-combiner`.

## [0.20.0] - 2026-11-03

### Changed

- The URL the extension dials comes from `PI_MCP_COMBINER_HOST` read as the
  combiner's bind setting: the first entry of a comma-separated list, loopback for a
  wildcard (`0.0.0.0` / `::`) or `auto` (tailnet), and IPv6 addresses bracketed.

## [0.18.0] - 2026-11-01

### Added

- **`structuredContent` passthrough**: an upstream tool's `outputSchema` is declared
  on the native tool (pi ≥ 1.2), and its `structuredContent` rides the tool result, so
  codemode scripts get typed values instead of re-parsing text. Older hosts ignore it.

Other releases from 0.17.0 to 0.21.2 changed only the combiner (see
`combiner/CHANGELOG.md`).

## [0.17.0] - 2026-10-08

### Changed

- **Tool-result truncation spills instead of cutting** (`client/render.ts`): text over the
  `maxResultChars` cap (default 16 KiB, setting `extensions/mcp-combiner.json`, env
  `PI_MCP_COMBINER_MAX_RESULT_CHARS`) gets a surrogate-safe head + a notice naming a 0600
  temp file with the FULL text (`/tmp/pi-mcp-*/<tool>.txt`) — recoverable via `read`, never
  a mid-JSON dead end; `full_output_path` rides the result `details` (bash parity). Applied
  on every surface: `mcp()` router calls, the native tool surface (previously UNGUARDED —
  full 80 KB+ results went straight to the model), and `read_*` resources.
- **Image-block handling unified** — images stay real `{type:"image"}` blocks on all call
  paths (`mcp()`, native surface, `read_*`); the non-vision omission note (read-tool
  parity, via the new guarded `safeModel` read in `runner-stale.ts`) replaces the old
  pre-emptive flattening in the `mcp()` router. `widget-support.ts` re-exports the shared
  guarded `renderResourceResult` from `render.ts` instead of its own 16 KB cut.

## [0.16.0] - 2026-10-04

### Removed

- **The legacy SDK client** (`@modelcontextprotocol/client`) — native mode is the
  only mode. The transport is `@earendil-works/pi-mcp` (peer dep, `^1.0.2`):
  Pi's own MCP client library on our owned connection.
- `mcpScript` (pi's built-in codemode is the superset), the `directTools`
  search-promote (native exposure + `tool_search` replace it), and the legacy
  resource sync (native resources own the interactive `read_*` surface).
- `mode` and `scriptMode` settings — moot.

### Added

- `client/types.ts` — the `CombinerConnection` interface + shared summary types;
  the native connection `implements` it, so surface modules depend on the
  contract, not the implementation.
- `exposeResources` gating for the native resource surface.

### Fixed

- Native surface registration deferred to `session_start` (post-token): a
  factory-time run logged "no grouping token set" startup warnings, and the
  wiring's `setHooks` replaced the activation's hook outright (last-wins).
- Zombie connection teardown on `/reload`: pi re-imports the module without
  firing `session_shutdown`, so the old instance's stream stayed alive and
  competed for elicit routing. A process-global registry survives re-imports;
  the fresh factory closes prior connections.
- The native connection's stale-session retry covers **every** request method,
  not just `callTool` — `listTools`/`listPrompts`/`getPrompt`/`listResources`/
  `readResource` were bare, so the router's listTools-first lookup surfaced raw
  `McpSessionExpiredError` after a combiner bounce and never reset the client,
  wedging the connection until reload.
- Review-driven fixes (dev native-MCP review): `callTool` retries transport
  failures **stale-only** (typed `McpSessionExpiredError` — raised before the
  server executes anything), so a tool error's own text can no longer trigger a
  re-run of a destructive tool; connection races closed (a reset no longer
  publishes an in-flight laggard connect, resets target the failing client only,
  and a slow connect that loses its timeout race is closed instead of leaking an
  untracked stream); pi's cancel signal now actually rides into `callTool`
  (upstream work + widget holds abort); per-project `directTools`/filter ride as
  per-pass getters (worktrees get their own exposure); `read_*` tool UI URLs are
  computed per call (stale session tokens no longer baked in at registration);
  the panel's async rejection is handled to stderr; debug logging is opt-in via
  `PI_MCP_COMBINER_NC_DBG=1`; removed tools are re-registered `hidden`, and
  unchanged declarations skip re-registration via schema-signature diffing;
  tool results map through pi-mcp's `toLlmContent` (real image blocks); README
  documents the codemode activation quirk; lockfile regenerated; three
  transport-level regression tests added (fake combiner over pi-mcp's
  in-memory pair).
- Runner-stale guard (pi earendil-works/pi#10599): pi's `reload()` invalidates
  the extension runner BEFORE replacing it — a window of seconds in which any
  guarded ctx read throws. Tool executes now read `ctx.hasUI` ONCE at entry
  through a throwing-getter-safe helper (`client/runner-stale.ts`) and never
  re-read past an await: a call that already executed can no longer be turned
  into a rejection (whose model-retry would re-execute it). Runner-stale errors
  that do surface get a recovery hint naming what happened and when retrying
  is safe, instead of pi's raw assertion text. No auto-retry for this class:
  unlike transport staleness, execution may already have happened.
- Stale-ctx sweep (pi 1.0's hard assertion on `ctx.ui` reads from async
  callbacks): every async callback now uses a **captured ui object** (the
  getter asserts; the object doesn't), catches degrade to stderr and never
  re-enter the getter, and the second logger (`makeLog`) captures at call
  time + guards its notify. The slash-command paths (status, meta-tool
  calls, the panel) audited: all async `ctx.ui` re-entries eliminated.
- Stale-ctx crash on `/reload` (pi 1.0's hard assertion): the teardown's
  reset-reason log rode the orphaned instance's captured `ctx.ui` — an
  uncaught throw in the fire-and-forget microtask killed pi. `clientLog`
  now degrades a stale UI capture to stderr instead of throwing, and the
  teardown retires the whole instance (footer timer, UI captures, connection).

## [0.15.0] - 2026-10-03

### Added

- **Native mode** (`"mode": "native"`): per-tool pi-1.0 registration
  (exposure/namespace/annotations) via the pi-mcp transport on an owned
  connection — same custody token, elicitation capability advertised and
  bridged. The `mcp` router registers alongside in both modes (tool_call
  gates recognize the `mcp` tool name).
- `docs/call-chain.md` — the layer map, naming, and token custody for a call
  in both modes, with the pi-native contrast.

### Fixed

- Connection-wide one-shot stale-session retry: a combiner restart mid-flight
  surfaced raw "Session not found" from the router's cache refresh before any
  `callTool` ran.
- Recovery-hint error messages name what died, what survives, and the retry.

### Changed

- The legacy client factored to `src/legacy-client/` (deletion checklist in
  its README); shared modules extracted to `src/client/`.

## [0.14.0] - 2026-09-17

### Added

- **The client half** — the extension speaks MCP to the combiner itself:
  the `mcp()` proxy tool (search/describe/call/list/status), `mcpScript`
  batching, `read_<resource>` tools with `ui://` detection and widget
  auto-open, prompt slash commands (`mcp__<server>__<name>`), the elicitation
  bridge (SDK structured dialogs), the footer chip, and the `/mcp-combiner`
  panel + control verbs.
- **Interactive widgets (Stage 2)** — widget-bound tool results hold the call;
  the extension auto-opens the widget URL from the hold notification
  (`uiAutoOpen` setting, default true) and the combiner streams the data.
- **directTools** — allowlist + search-mode tool promotion.
- `uiAutoOpen` / `mcpFooterKey` settings; adapter tri-state (`auto`/`true`/
  `false`) coexistence with pi-mcp-adapter.
- Conformance tests ported from pi-mcp-adapter (51 passing).

### Fixed

- TUI renderers implement `invalidate()` — pi-tui's Component contract; the
  missing method crashed pi on transcript invalidation (session restore,
  theme change, resize).

## [0.13.4] and earlier

See the [GitHub releases](https://github.com/georgeharker/mcp-companion/releases).
