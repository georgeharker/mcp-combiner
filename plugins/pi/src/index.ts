// Pi extension: run the `mcp-combiner` MCP aggregator via the `sharedserver` CLI so
// it is available to Pi — and BE the MCP client for it (the adapter half).
//
// Two halves, the counterpart of the Claude Code and OpenCode plugins:
//
//   1. Process — drive `sharedserver use … -- <combiner> --mcp --config … --port …` so
//      the combiner is running and refcounted, SHARED with any other client (Claude
//      Code, OpenCode, Neovim) that uses the same sharedserver name. Launched in
//      `session_start` (Pi forbids background startup from the factory) and released in
//      `session_shutdown` — but only on `reason === "quit"`, since reload/new/resume/
//      fork keep the same Pi process alive and a fresh `session_start` re-attaches.
//   2. Client — speak MCP directly to the combiner (streamable HTTP, per-Pi-session
//      grouping token, elicitation bridge) and register the mcp() proxy tool, so no
//      second package (pi-mcp-adapter) is needed. Connection + per-project exposure
//      come from the shared MCP config ladder (.pi/mcp.json & co.); Pi-side knobs
//      (tool name, lazy/eager, kill-switch) from
//      $PI_CODING_AGENT_DIR/extensions/mcp-combiner.json. See docs/adapter-design.md.
//
//   3. Instructions — append the combiner's tool-discovery directive to the system
//      prompt via `before_agent_start` (analogue of the CC plugin's SessionStart
//      additionalContext and the OpenCode plugin's system.transform hook). The combiner
//      also serves the same text as its MCP `instructions` — belt to that braces.
//
// The sharedserver resolution/fetch and the combiner command ladder are ported
// faithfully from plugins/opencode/src/index.ts — same floor, same warnings, same
// degrade-rather-than-die behaviour, so a user running several clients gets one answer
// to "which combiner / which sharedserver am I on, and why".

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type {
    AutocompleteItem,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    SessionShutdownEvent,
    SessionStartEvent,
    CommandSpec,
} from "./pi.js"
import { CombinerProcess, DEFAULT_PORT, env, type LogFn } from "./combiner-process.js"
import { NativeCombinerConnection, activateNativeMode, waitForRegistration } from "./native/index.js"
// ── TOOL SURFACE ───────────────────────────────────────────────────────────────
// The mcp router (search/describe/call on the owned connection) + the native
// per-tool registration (pi-1.0 exposure/namespace/annotations) + interactive
// read_<resource> tools. Shared surface modules live in src/client/; the transport
// is src/native/combiner-connection.ts (pi-mcp, peer dep).
import { createMcpTool } from "./client/proxy-tool.js"
import { openInBrowser } from "./client/widget-support.js"
import { agentDir, loadSettings, settingsPath } from "./client/settings.js"
import { resolveSessionConfig, type SessionConfig } from "./client/config-ladder.js"
import {
    collectionCall,
    collectionsText,
    metaToolCall,
    statusText as controlStatusText,
} from "./client/control.js"
import { openCombinerPanel } from "./client/panel.js"
import { syncPromptCommands } from "./client/prompts.js"
import { updateFooter, type RegisterProgress } from "./client/footer.js"
import { matchesKey } from "@earendil-works/pi-tui"

// ── the tool-discovery directive ───────────────────────────────────
// Appended to the system prompt so the agent knows combined tools arrive under a
// `<server>_` prefix (and, through pi-mcp-adapter, are reached via its `mcp()` proxy or
// promoted `directTools`), and looks before deciding a capability is absent. Canonical
// source: CLAUDE.md.example at the repo root; a release-time `prepack` copies it to this
// package's root as instructions.txt (see package.json). A dev/unbuilt run without the
// copy falls back to empty and simply injects nothing.
const COMBINER_DIRECTIVE: string = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        // dist/index.js and the packed instructions.txt both sit one level up from here
        // when built (dist/), and the source layout mirrors it (src/ → package root).
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()
// First line of the directive — used to detect an already-appended prompt so repeated
// `before_agent_start` turns do not stack duplicate copies.
const DIRECTIVE_MARKER = COMBINER_DIRECTIVE.split("\n", 1)[0] ?? ""

// ── env configuration ──────────────────────────────────────────────
// The PI_MCP_COMBINER_* namespace mirrors the Claude plugin's CLAUDE_MCP_COMBINER_* and
// the OpenCode plugin's OPENCODE_MCP_COMBINER_*, so a user running several clients keeps
// one namespace per client rather than options in one place and env vars in another.
// env(), the combiner command/config/port resolution and the whole sharedserver
// lifecycle live in combiner-process.ts.

// ── the extension ──────────────────────────────────────────────────

export default function mcpCombiner(pi: ExtensionAPI): void {
    const notify = env("PI_MCP_COMBINER_NOTIFY") !== "false"
    const wantInstructions = env("PI_MCP_COMBINER_INSTRUCTIONS") !== "false"
    const manage = env("PI_MCP_COMBINER_MANAGE") !== "false"

    // Host-owned mode: a set $MCP_COMPANION_COMBINER_URL means an editor/host (e.g.
    // Neovim) already owns and refcounts the combiner. Then we NEVER launch — only the
    // instructions half runs. The host never sets a port var, mirroring the sibling
    // plugins' distinction.
    const hostOwned = env("MCP_COMPANION_COMBINER_URL") !== undefined && env("PI_MCP_COMBINER_PORT") === undefined

    // ── client half: be the adapter ──────────────────────────────
    // Connect to the combiner over streamable HTTP and register the mcp() proxy tool
    // (docs/adapter-design.md). Runs in host-owned mode too — the host owns the
    // process; we still own OUR connection to it. Connection URL precedence:
    // PI_MCP_COMBINER_URL → settings file → shared-ladder combiner entry → serve
    // defaults (host:port/mcp).
    const settings = loadSettings()
    const toolName = env("PI_MCP_COMBINER_TOOL_NAME") ?? settings.toolName

    // Client-half gate: explicit env > settings file > "auto" (default) — off when
    // pi-mcp-adapter is detected installed. Existing adapter users therefore upgrade
    // to zero behaviour change; fresh installs get the one-package experience.
    const adapterInstalled = adapterPackageInstalled()

    function resolveAdapterChoice(): boolean | "auto" {
        const e = env("PI_MCP_COMBINER_ADAPTER")?.toLowerCase()
        if (e === "off" || e === "false" || e === "0") return false
        if (e === "on" || e === "true" || e === "1") return true
        if (e === "auto") return "auto"
        return settings.adapter
    }
    const adapterChoice = resolveAdapterChoice()
    const adapterEnabled = adapterChoice === "auto" ? !adapterInstalled : adapterChoice
    let adapterOffReason: string | undefined
    if (!adapterEnabled) {
        adapterOffReason =
            adapterChoice === false
                ? "explicitly disabled (adapter:false or PI_MCP_COMBINER_ADAPTER=off)"
                : `pi-mcp-adapter detected installed — leaving MCP to it (set "adapter": true in ${settingsPath()} to take over)`
    }

    // The client log starts on stderr; once a session binds, mirror to ctx.ui.notify.
    let uiNotify: ((message: string, level?: "info" | "warn" | "error") => void) | undefined
    const clientLog: LogFn = (level, message) => {
        const line = `mcp-combiner: ${message}`
        if (uiNotify) {
            let uiLevel: "info" | "warn" | "error" = "info"
            if (level === "warn") uiLevel = "warn"
            else if (level === "error") uiLevel = "error"
            try {
                uiNotify(line, uiLevel)
            } catch {
                // The captured ctx went stale (reload/session replacement kills
                // extension ctxs — pi 1.0 asserts on use). Degrade to stderr and
                // drop the capture so later logs skip the dead path entirely:
                // an async log line (e.g. a superseded connection's reset) must
                // NEVER crash the process.
                uiNotify = undefined
                process.stderr.write(`${line}\n`)
            }
        } else if (level !== "info") {
            process.stderr.write(`${line}\n`)
        }
    }

    let connection: NativeCombinerConnection | undefined
    // The combiner process, when we manage it (combiner-process.ts): connects wait
    // for its first attach (`ready`), and ask it between refused retries. Host-owned
    // or unmanaged combiners are someone else's: connect only.
    const combinerProcess = manage && !hostOwned ? new CombinerProcess(clientLog) : undefined
    // Stops CombinerProcess.watch at session_shutdown / reload teardown.
    let stopCombinerWatch: (() => void) | undefined
    let nativeSurface: ReturnType<typeof activateNativeMode> | undefined
    // Set once the footer exists (the `if (connection)` block below); the native
    // surface, created first, reports registration progress through it.
    let onToolProgress: ((done: number, total: number) => void) | undefined
    // Session-config HOLDER: everything the ladder shapes, resolved against a SESSION
    // cwd (worktree subagents / project switches get their own project layers).
    // Factory-time resolution with process.cwd() is the pre-session default; every
    // session_start re-resolves with ctx.cwd and updates the holder in place.
    let sessionCfg: SessionConfig | undefined
    const envUrl = env("MCP_COMPANION_COMBINER_URL") ?? env("PI_MCP_COMBINER_URL")
    const connInputsFor = (cfg: SessionConfig) => ({
        baseUrl: cfg.baseUrl,
        bearerTokenEnv: cfg.bearerTokenEnv ?? "MCP_COMBINER_AUTH_TOKEN",
        urlToken: cfg.urlToken,
    })
    if (adapterEnabled) {
        sessionCfg = resolveSessionConfig({
            cwd: process.cwd(),
            envUrl,
            settingsUrl: settings.url,
            defaultUrl: defaultCombinerUrl(),
        })
        // Zombie teardown: pi re-imports the extension module on /reload WITHOUT
        // firing session_shutdown (the session persists, no unload hook exists), so
        // the previous instance's McpClient + SSE stream would live on and compete
        // for the token's elicit routing. A process-global registry survives module
        // re-imports: the fresh factory closes every prior live connection first.
        const g = globalThis as { __piMcpCombinerDispose?: Array<() => void> }
        // The teardown closures below (from prior factory runs, orphaned by pi's
        // reload-without-session_shutdown) know how to fully retire their instance:
        // clear the footer timer, drop the captured UI ctx (stale-ctx reads on it
        // are a hard pi-1.0 assertion), and close the connection.
        for (const dispose of g.__piMcpCombinerDispose ?? []) {
            try {
                dispose()
            } catch {
                // teardown must never break the fresh instance's load
            }
        }
        g.__piMcpCombinerDispose = []
        connection = new NativeCombinerConnection(connInputsFor(sessionCfg), clientLog)
        if (combinerProcess) {
            const proc = combinerProcess
            const conn = connection
            conn.setStartGate(proc.ready)
            conn.setLivenessCheck(() => proc.onRefused())
        }
        // The mcp router registers alongside the per-tool native surface (the same
        // owned connection — not a second proxy): tool_call gates such as
        // pi-permission-system and pi-subagents recognize MCP calls by the tool
        // name "mcp", and the router keeps a discovery/search surface.
        pi.registerTool(
            createMcpTool({
                connection,
                toolName,
                maxResultChars: settings.maxResultChars,
                getServerFilter: () => sessionCfg?.serverFilter,
            }),
        )
        // Per-tool surface + interactive read_*. The FIRST registration runs at
        // session_start (post-setToken — the connection refuses to connect
        // without a grouping token); list_changed re-syncs ride resyncAll below.
        nativeSurface = activateNativeMode(pi, {
            onToolProgress: (done, total) => onToolProgress?.(done, total),
            connection,
            // Getters, not snapshots: worktrees / project switches re-resolve
            // sessionCfg per session_start, and a load-time value would apply the
            // wrong allow/deny set and directTools spec to later sessions.
            directSpec: () => sessionCfg?.directSpec,
            serverFilter: () => sessionCfg?.serverFilter,
            exposeResources: settings.exposeResources !== false,
            uiAutoOpen: settings.uiAutoOpen !== false,
            warnLargeDirectExposure: settings.warnLargeDirectExposure,
            maxResultChars: settings.maxResultChars,
            log: clientLog,
        })
    }

    if (!adapterEnabled && notify) {
        // One info line per session when auto/legacy mode is active, so the state is
        // visible without running /mcp-combiner status.
        pi.on("session_start", (_event, ctx) => {
            ctx.ui?.notify?.(`mcp-combiner: client half off — ${adapterOffReason}`, "info")
        })
    }

    if (connection) {
        const conn = connection // captured non-optional for the handlers below
        // The connection keeps itself up (reconnect after a drop, background retry after
        // a failed connect; combiner-connection.ts). The combiner process, when we
        // manage it, reports process events: its going away triggers a reconnect, and
        // the reconnect asks it (respawn within the cap, or attach to a new process).
        const arm = () => {
            conn.reopen()
            conn.setAutoReconnect(true)
            if (combinerProcess && !stopCombinerWatch)
                stopCombinerWatch = combinerProcess.watch(() => conn.reconnect("the combiner process exited"))
        }
        arm()
        const registeredPrompts = new Set<string>()
        let footerTimer: NodeJS.Timeout | undefined
        let uiCtx: ExtensionContext["ui"] | undefined

        // Full instance teardown, callable from a LATER factory run (pi re-imports
        // the module on /reload without firing session_shutdown): clears this
        // instance's footer timer, drops its captured UI ctx (stale-ctx reads are
        // a hard pi-1.0 assertion), and closes its connection. The registry lives
        // on globalThis — the one thing that survives a module re-import.
        const teardownGlobal = globalThis as { __piMcpCombinerDispose?: Array<() => void> }
        ;(teardownGlobal.__piMcpCombinerDispose ??= []).push(() => {
            if (footerTimer) clearInterval(footerTimer)
            footerTimer = undefined
            uiCtx = undefined
            uiNotify = undefined
            stopCombinerWatch?.()
            stopCombinerWatch = undefined
            nativeSurface?.dispose()
            void conn.close("superseded by a newer extension instance (reload)")
        })

        // Native registration progress (undefined when no pass is registering).
        let registering: RegisterProgress | undefined
        let lastProgressPaint = 0
        const refreshFooter = () => {
            if (uiCtx)
                void updateFooter(conn, uiCtx, settings.mcpFooterKey, settings.mcpFooterStatus, () => registering).catch(
                    () => undefined,
                )
        }
        // Called after every registration slice. Repaint at most every 500 ms (each
        // repaint fetches /health) and always on the final slice.
        onToolProgress = (done, total) => {
            const finished = done >= total
            // A pass's last slice reports (total, total) and its end reports (0, 0):
            // repaint for the first "finished" only.
            const wasRegistering = registering !== undefined
            registering = finished ? undefined : { done, total }
            if (finished && !wasRegistering) return
            const now = Date.now()
            if (finished || now - lastProgressPaint >= 500) {
                lastProgressPaint = now
                refreshFooter()
            }
        }
        const syncPrompts = () => {
            if (!settings.prompts) return
            void syncPromptCommands(pi, conn, toolName, registeredPrompts, (m, l) => clientLog(l, m)).catch(
                () => undefined,
            )
        }
        const resyncAll = () => {
            // Prompts + footer re-sync on: list_changed notifications, reconnect
            // catch-up (fired by the connection itself), AND session_start — a
            // reload/re-enter with an UNCHANGED token skips any reconnect, so
            // without this the surfaces would only ever update on notifications.
            nativeSurface?.run()
            syncPrompts()
            refreshFooter()
        }
        conn.setHooks({
            onToolsChanged: resyncAll,
            onStateChange: (state) => {
                // A different combiner process now (someone restarted it)? Attach to
                // it: same function, same ref (CombinerProcess.onReconnected).
                if (state === "connected") void combinerProcess?.onReconnected().catch(() => undefined)
                refreshFooter()
            },
            // Stage 2: the widget hold announces its UI URL mid-call — open it.
            onWidgetUrl: (url) => {
                if (settings.uiAutoOpen !== false) openInBrowser(url)
            },
        })

        pi.on("session_start", (event, ctx) => {
            try {
                // Re-arm auto-reconnect and the process watch if a host reused this
                // instance after a session_shutdown (idempotent; pi 1.6 doesn't reuse).
                arm()
                // SAFETY: capture ctx.ui ONCE, first — the getter asserts staleness,
                // the object does not; every async closure below uses the object only.
                const ctxUi = ctx.ui
                uiNotify = notify && ctx.hasUI && ctxUi?.notify ? (m, l) => ctxUi?.notify?.(m, l) : undefined
                uiCtx = ctxUi
                conn.bindUi({ hasUI: ctx.hasUI && Boolean(ctxUi?.select && ctxUi?.input), ui: ctxUi })
                // Per-session config re-resolution: worktrees and project switches read
                // THEIR project layers (.mcp.json / .pi/mcp.json); a changed URL rebinds.
                sessionCfg = resolveSessionConfig({
                    cwd: ctx.cwd,
                    envUrl,
                    settingsUrl: settings.url,
                    defaultUrl: defaultCombinerUrl(),
                })
                conn.rebind(connInputsFor(sessionCfg))
                conn.setToken(sessionToken(event, ctx))
                // The native per-tool/resource registration runs HERE — post-setToken,
                // the first moment the grouping token exists (a factory-time run
                // would fail with "no grouping token set"). Subsequent re-syncs ride
                // resyncAll via onToolsChanged.
                nativeSurface?.run()
                // Per-project exposure: pending filters apply server-side before first connect.
                const filter = sessionCfg.serverFilter
                if (filter) {
                    void conn
                        .applyFilter(filter)
                        .catch((e) =>
                            clientLog("warn", `filter apply failed: ${e instanceof Error ? e.message : String(e)}`),
                        )
                }
                if (sessionCfg.otherServers.length && !adapterPackageInstalled()) {
                    clientLog(
                        "info",
                        `note: ${sessionCfg.otherServers.length} non-combiner server(s) in the mcp.json ladder are not ` +
                            `connected by this extension (${sessionCfg.otherServers.join(", ")})`,
                    )
                }
                // Prompts/resources need the connection at session start to register their
                // surfaces, so they imply an eager connect; otherwise lazy waits for the
                // first call. A directTools allowlist needs the surface from turn one too.
                if (
                    settings.lazy === "eager" ||
                    settings.prompts ||
                    settings.exposeResources ||
                    Array.isArray(sessionCfg.directSpec)
                ) {
                    void conn
                        .ensureConnected()
                        .then(() => {
                            refreshFooter()
                            syncPrompts()
                        })
                        .catch(() => undefined)
                }
                refreshFooter()
                if (footerTimer) clearInterval(footerTimer)
                footerTimer = setInterval(refreshFooter, 30_000)
                footerTimer.unref?.()
                if (toolName === "mcp" && adapterInstalled) {
                    clientLog(
                        "warn",
                        "adapter forced ON while pi-mcp-adapter is installed — both register an `mcp` tool. " +
                            "Rename ours (settings toolName / PI_MCP_COMBINER_TOOL_NAME), uninstall the adapter, " +
                            "or revert to adapter auto (PI_MCP_COMBINER_ADAPTER=auto)",
                    )
                }
            } catch (e) {
                // A throwing session_start handler would silently kill the footer and
                // surface registration for the whole session — surface it instead.
                clientLog("error", `session setup failed: ${e instanceof Error ? e.message : String(e)}`)
            }
        })
        // A turn that starts while tools are still registering (or the first connect
        // is still retrying) would see only part of the catalog. pi awaits
        // before_agent_start handlers and reads the live tool set afterwards, so
        // waiting here puts every tool in the turn. The registration keeps yielding
        // while we wait, so the UI stays live. pi has no abort for this hook today
        // (Esc during the wait takes effect when it ends); ctx.signal is honoured if
        // a future pi provides one.
        pi.on("before_agent_start", async (_event, ctx) => {
            const surface = nativeSurface
            if (!surface?.busy()) return
            // Still connecting: wait only for a combiner we just attached (it is
            // starting up). A host-owned/unmanaged combiner that's down, or one whose
            // attach failed, would only stall the turn for the whole retry budget.
            const connecting = conn.state !== "connected"
            if (connecting && !combinerProcess?.attached) return
            const ui = ctx.ui
            const notice = setTimeout(() => {
                if (notify)
                    ui?.notify?.(
                        connecting
                            ? "mcp-combiner: waiting for the combiner to come up…"
                            : "mcp-combiner: waiting for MCP tools to finish registering…",
                        "info",
                    )
            }, 250) // brief waits (a list_changed resync) stay silent
            notice.unref()
            const started = Date.now()
            const outcome = await waitForRegistration(surface, {
                signal: ctx.signal,
                // pi has no abort for this hook: end the wait on Esc / Ctrl+C / Ctrl+D
                // ourselves (pi still sees the key and cancels or quits as usual).
                // A single Ctrl+C only clears the editor in pi; its second press within
                // 500 ms quits, so that is what ends the wait.
                onInterrupt: (interrupt) => {
                    let lastCtrlC = 0
                    return (
                        ui?.onTerminalInput?.((data) => {
                            if (matchesKey(data, "ctrl+c")) {
                                const now = Date.now()
                                if (now - lastCtrlC < 500) interrupt()
                                lastCtrlC = now
                            } else if (matchesKey(data, "escape")) interrupt()
                            // Ctrl+D quits only on an empty editor; with text it deletes forward.
                            else if (matchesKey(data, "ctrl+d") && !(ui?.getEditorText?.() ?? "")) interrupt()
                            return undefined
                        }) ?? (() => undefined)
                    )
                },
            })
            clearTimeout(notice)
            const waitedMs = Date.now() - started
            const waited = `${(waitedMs / 1000).toFixed(1)} s`
            if (outcome === "timeout") {
                clientLog(
                    "warn",
                    `tool registration still running after ${waited}; the rest join from the turn's next model request`,
                )
            } else if (waitedMs >= 250) {
                // Brief waits (a list_changed resync) stay silent, like the notice.
                clientLog("info", `turn waited ${waited} for tool registration (${outcome})`)
            }
        })

        pi.on("session_shutdown", () => {
            if (footerTimer) {
                clearInterval(footerTimer)
                footerTimer = undefined
            }
            // Stop a registration pass in flight before pi retires this session's
            // extension API (pi 1.6 creates a fresh instance for the next session).
            stopCombinerWatch?.()
            stopCombinerWatch = undefined
            nativeSurface?.dispose()
            void conn.close("session shutdown")
        })
    }

    // ── instructions: appended every turn (analogue of the sibling plugins) ──
    pi.on("before_agent_start", (event) => {
        if (!wantInstructions || !COMBINER_DIRECTIVE) return
        // Do not stack duplicates across turns: the chained prompt carries forward.
        if (DIRECTIVE_MARKER && event.systemPrompt.includes(DIRECTIVE_MARKER)) return
        return { systemPrompt: `${event.systemPrompt}\n\n${COMBINER_DIRECTIVE}` }
    })

    // ── /mcp-combiner command: inspect + drive the extension and the combiner ──
    // verbs: status (combiner health + this session's view), enable/disable/
    // restart-server <srv> (combiner meta-tools through our client), collections /
    // collection on|off|reset <name> (collections for this chat only), system-prompt
    // (show the injected directive), install-config [path] (write the shared mcp.json
    // combiner entry — legacy pairing with pi-mcp-adapter, now also read by our ladder).
    const combinerCommandSpec: CommandSpec = {
        description:
            "mcp-combiner — verbs: status, connect, enable <srv>, disable <srv>, restart-server <srv>, collections, collection on|off|reset <name>, system-prompt, install-config [path]",
        getArgumentCompletions: (prefix) => completeVerbs(prefix),
        handler: (args, ctx) => {
            const [verb, ...rest] = args.trim().split(/\s+/)
            const v = verb ?? ""
            // SAFETY: ctx.ui's GETTER asserts staleness; the object it returns does
            // not — capture once, use the object in async paths (stale after a
            // reload is a no-op render, not a crash).
            const ctxUi = ctx.ui
            // SAFETY: the shared async-notify shape (notify via the captured object,
            // degrade to stderr) — a catch must never re-enter the same stale ctx it
            // is handling, so every async .then/.catch funnels through here.
            const uiNotifySafe = (msg: string, level: "info" | "warn" | "error"): void => {
                try {
                    ctxUi?.notify?.(msg, level)
                } catch {
                    process.stderr.write(`mcp-combiner: ${msg}\n`)
                }
            }
            if (v === "panel") {
                if (!connection) {
                    ctx.ui?.notify?.(
                        `mcp-combiner: client half off${adapterOffReason ? ` — ${adapterOffReason}` : ""}`,
                        "warn",
                    )
                    return
                }
                // Async (it opens a held tool session); its rejection must be handled
                // via STDERR — not via the stale getter this handler was entered for.
                void openCombinerPanel(
                    {
                        connection,
                        toolName,
                        getToken: () => connection.sessionToken,
                        getFilter: () => sessionCfg?.serverFilter,
                        notify: (m, l) => ctxUi?.notify?.(m, l),
                    },
                    ctxUi,
                ).catch((e) => process.stderr.write(`mcp-combiner: panel failed (${e instanceof Error ? e.message : String(e)})\n`))
                return
            }
            if (v === "" || v === "status") {
                if (!connection) {
                    ctx.ui?.notify?.(
                        `mcp-combiner: client half off${adapterOffReason ? ` — ${adapterOffReason}` : ""}`,
                        "warn",
                    )
                    return
                }
                void controlStatusText(connection, ctx.cwd)
                    .then((t) => uiNotifySafe(t, "info"))
                    .catch((e) =>
                        uiNotifySafe(`status failed (${e instanceof Error ? e.message : String(e)})`, "error"),
                    )
                return
            }
            if (v === "connect") {
                if (!connection) {
                    ctx.ui?.notify?.(
                        `mcp-combiner: client half off${adapterOffReason ? ` — ${adapterOffReason}` : ""}`,
                        "warn",
                    )
                    return
                }
                const conn = connection
                if (conn.state === "connected") {
                    uiNotifySafe(`already connected to ${sessionCfg?.baseUrl ?? "the combiner"}`, "info")
                    return
                }
                uiNotifySafe(`connecting to ${sessionCfg?.baseUrl ?? "the combiner"}…`, "info")
                void (async () => {
                    // A combiner we manage: re-attach (launching it if it isn't
                    // running), lifting any respawn stop — the user asked for it.
                    if (combinerProcess) await combinerProcess.retryNow(clientLog)
                    await conn.connectNow()
                    const tools = await conn.listTools(true).catch(() => [])
                    return `connected — ${tools.length} tools`
                })()
                    .then((t) => uiNotifySafe(t, "info"))
                    .catch((e) =>
                        uiNotifySafe(
                            `connect failed (${e instanceof Error ? e.message : String(e)}); still retrying in the background`,
                            "error",
                        ),
                    )
                return
            }
            if (v === "enable" || v === "disable" || v === "restart-server") {
                const server = rest[0]
                if (!connection) {
                    ctx.ui?.notify?.("mcp-combiner: client half disabled", "warn")
                    return
                }
                if (!server) {
                    ctx.ui?.notify?.(`mcp-combiner: ${v} needs a server name`, "warn")
                    return
                }
                void metaToolCall(connection, v, server)
                    .then((t) => uiNotifySafe(t, "info"))
                    .catch((e) => uiNotifySafe(`${v} failed (${e instanceof Error ? e.message : String(e)})`, "error"))
                return
            }
            if (v === "collections" || v === "collection") {
                if (!connection) {
                    ctx.ui?.notify?.("mcp-combiner: client half disabled", "warn")
                    return
                }
                const [action, name] = rest
                if (v === "collections" || !action) {
                    void collectionsText(connection)
                        .then((t) => uiNotifySafe(t, "info"))
                        .catch((e) =>
                            uiNotifySafe(`collections failed (${e instanceof Error ? e.message : String(e)})`, "error"),
                        )
                    return
                }
                if (action !== "on" && action !== "off" && action !== "reset") {
                    ctx.ui?.notify?.("mcp-combiner: collection on|off|reset <name>", "warn")
                    return
                }
                if (!name) {
                    ctx.ui?.notify?.(`mcp-combiner: collection ${action} needs a collection name`, "warn")
                    return
                }
                void collectionCall(connection, action, name)
                    .then((t) => uiNotifySafe(t, "info"))
                    .catch((e) =>
                        uiNotifySafe(`collection ${action} failed (${e instanceof Error ? e.message : String(e)})`, "error"),
                    )
                return
            }
            if (v === "system-prompt") {
                showDirective(ctx, "mcp-combiner", COMBINER_DIRECTIVE, wantInstructions)
                return
            }
            if (v === "install-config") {
                installConfig(ctx, rest.join(" ").trim() || undefined)
                return
            }
            ctx.ui?.notify?.(`mcp-combiner: unknown verb "${v}". Try: ${COMMAND_VERBS.join(", ")}`, "warn")
        },
    }
    pi.registerCommand("mcp-combiner", combinerCommandSpec)

    // NOTE: we deliberately do NOT register "/mcp" — pi's built-in MCP extension
    // owns that command and stays the native-mode transport; our surfaces ("mcp"
    // router tool, direct <server>_<tool>, read_*) don't collide with the builtin's
    // (codemode / tool-search / read_mcp_resource). Registering "/mcp" would
    // displace the builtin at load (pi's loader drops replaceable built-ins on
    // command-name collision) and with it the native support we're migrating
    // toward.

    if (hostOwned || !manage) {
        // Registration is static (mcp.json) and the combiner is someone else's to run:
        // nothing to launch. The instructions handler above still applies.
        return
    }

    // ── process: launch on session_start, release on session_shutdown("quit") ──
    // Pi forbids starting background resources from the factory, so the attach is
    // deferred to session_start. It fires again on reload/new/resume/fork within the
    // same process; CombinerProcess keeps one ref per pi process, so re-entry is a
    // no-op. Everything else about the process lives in combiner-process.ts.
    const proc = combinerProcess
    if (!proc) return // unreachable: combinerProcess exists exactly when manage && !hostOwned
    pi.on("session_start", async (_event, ctx) => {
        await proc.start(makeLog(ctx, notify))
    })

    pi.on("session_shutdown", (event: SessionShutdownEvent) => {
        // Only "quit" means the Pi process is actually leaving. reload/new/resume/fork
        // keep the process alive and a fresh session_start re-attaches — releasing the
        // refcount on those would needlessly drop (and re-take) the shared combiner.
        if (event.reason === "quit") proc.quit()
    })
}

// ── helpers ────────────────────────────────────────────────────

// ── client-half helpers ──

/** Best-effort detection of an installed pi-mcp-adapter package (coexistence warn). */
function adapterPackageInstalled(): boolean {
    return existsSync(join(agentDir(), "npm", "node_modules", "pi-mcp-adapter"))
}

/** Grouping token for this Pi session: `pi-<sessionId>`. A resumed chat continues the
 *  previous chat's combiner identity (its parked upstream sessions survive); new/fork
 *  deliberately start fresh — a fork is a new chat, and sharing would clash upstream. */
function sessionToken(event: SessionStartEvent, ctx: ExtensionContext): string {
    if (event.reason === "resume" && event.previousSessionFile) {
        const m = basename(event.previousSessionFile).match(
            /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i,
        )
        if (m) return `pi-${m[1]}`
    }
    const sid = ctx.sessionManager?.getSessionId?.()
    return `pi-${sid ?? process.pid}`
}

// The verbs the extension's slash command understands. `system-prompt` shows the
// directive this extension injects — the show-command pattern from pi-custom-system-prompt,
// since `before_agent_start` injections are per-turn and never appear in Pi's own
// `/system-prompt` (which reports the base prompt only).
const COMMAND_VERBS = [
    "status",
    "connect",
    "panel",
    "enable",
    "disable",
    "restart-server",
    "collections",
    "collection",
    "system-prompt",
    "install-config",
]
function completeVerbs(prefix: string): AutocompleteItem[] | null {
    const p = prefix.trim()
    const matches = COMMAND_VERBS.filter((v) => v.startsWith(p))
    return matches.length ? matches.map((v) => ({ value: v, label: v })) : null
}

const SHOW_LIMIT = 1600
function showDirective(ctx: ExtensionCommandContext, label: string, directive: string, enabled: boolean): void {
    if (!directive) {
        ctx.ui?.notify?.(`${label}: no directive bundled (instructions.txt missing)`, "warn")
        return
    }
    const head = enabled
        ? `${label} directive — injected into the system prompt on every turn (before_agent_start):`
        : `${label} directive — injection is DISABLED this session; it would be:`
    const body =
        directive.length > SHOW_LIMIT
            ? `${directive.slice(0, SHOW_LIMIT)}\n\n… (${directive.length} chars total)`
            : directive
    ctx.ui?.notify?.(`${head}\n\n${body}`, "info")
}

// ── /mcp-combiner install-config: write the pi-mcp-adapter mcp.json entry ──
// A USER-INVOKED write (not the extension silently managing another extension's
// config): on request, merge the combiner entry into pi-mcp-adapter's mcp.json,
// wired for the combiner's optional inbound bearer auth:
//
//   { "url": "…/mcp", "auth": "bearer", "bearerTokenEnv": "MCP_COMBINER_AUTH_TOKEN" }
//
// - auth:"bearer" → the adapter attaches `Authorization: Bearer <token>` (it gates
//   the header on `auth === "bearer"` — NOT on bearerTokenEnv alone, and never when
//   auth is false/undefined), AND makes supportsOAuth() false so a wrong/missing
//   token surfaces as an honest 401 instead of a Dynamic-Client-Registration 404.
// - bearerTokenEnv → names the env var the token is read from at connect (nothing
//   written to disk). Combiner enforces only when MCP_COMBINER_AUTH_TOKEN is set on
//   ITS side; unset ⇒ open, the env var is unset here too so no header is sent.
// The one shape is correct whether or not auth is enabled.

function expandHome(p: string): string {
    if (p === "~") return homedir()
    if (p.startsWith("~/")) return join(homedir(), p.slice(2))
    return p
}

/** The address to REACH the combiner at, from its bind setting: the first entry of
 *  a comma-separated list, with loopback standing in for a wildcard or the
 *  `auto` (the tailnet) entry (both name where it listens, not an address to dial). */
function connectHost(bind: string | undefined): string {
    const first = (bind ?? "").split(",")[0]?.trim() ?? ""
    if (first === "" || first === "0.0.0.0" || first === "::" || first.toLowerCase() === "auto") {
        return "127.0.0.1"
    }
    return first.includes(":") ? `[${first}]` : first
}

/** The URL the adapter should reach the combiner at — the host-owned URL if an
 *  editor supervises it, else host:port/mcp with the same defaults the extension
 *  serves on. A grouping-path token, if wanted, is preserved from any existing
 *  entry rather than invented here. */
function defaultCombinerUrl(): string {
    const explicit = env("MCP_COMPANION_COMBINER_URL")
    if (explicit) return explicit
    const host = connectHost(env("PI_MCP_COMBINER_HOST"))
    let port = DEFAULT_PORT
    const raw = env("PI_MCP_COMBINER_PORT")
    if (raw !== undefined) {
        const n = Number(raw)
        if (Number.isInteger(n) && n > 0) port = n
    }
    return `http://${host}:${port}/mcp`
}

function installConfig(ctx: ExtensionCommandContext, pathArg?: string): void {
    const target = pathArg ? expandHome(pathArg) : join(homedir(), ".config", "mcp", "mcp.json")
    const key = "mcp-combiner"

    // Read + parse existing (tolerate absence; refuse to clobber non-JSON).
    let doc: Record<string, unknown> = {}
    if (existsSync(target)) {
        try {
            const parsed = JSON.parse(readFileSync(target, "utf8"))
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
                ctx.ui?.notify?.(`mcp-combiner: ${target} is not a JSON object; not overwriting`, "error")
                return
            }
            doc = parsed as Record<string, unknown>
        } catch (e) {
            ctx.ui?.notify?.(`mcp-combiner: ${target} is not valid JSON; not overwriting (${e})`, "error")
            return
        }
    }

    if (typeof doc.mcpServers !== "object" || doc.mcpServers === null) {
        doc.mcpServers = {}
    }
    const servers = doc.mcpServers as Record<string, Record<string, unknown>>
    const prev = (servers[key] ?? {}) as Record<string, unknown>
    const before = JSON.stringify(prev)
    // Preserve any existing url (e.g. a /mcp/<token> grouping path the user set)
    // and any other fields; only ensure the three auth-wiring keys.
    servers[key] = {
        ...prev,
        url: typeof prev.url === "string" && prev.url ? prev.url : defaultCombinerUrl(),
        auth: "bearer",
        bearerTokenEnv: "MCP_COMBINER_AUTH_TOKEN",
    }
    const existed = before !== "{}" && Object.keys(prev).length > 0
    const changed = before !== JSON.stringify(servers[key])

    try {
        mkdirSync(dirname(target), { recursive: true })
        writeFileSync(target, `${JSON.stringify(doc, null, 2)}\n`, "utf8")
    } catch (e) {
        ctx.ui?.notify?.(`mcp-combiner: failed to write ${target} (${e})`, "error")
        return
    }

    const what = existed ? (changed ? "updated" : "already configured") : "added"
    ctx.ui?.notify?.(
        `mcp-combiner: ${what} "${key}" in ${target}\n` +
            `Sends "Authorization: Bearer $MCP_COMBINER_AUTH_TOKEN" when that env var is set ` +
            `(auth:"bearer" both sends the token and suppresses OAuth probing). ` +
            `Run /reload so pi-mcp-adapter re-reads mcp.json.`,
        "info",
    )
}

function makeLog(ctx: ExtensionContext, notify: boolean): LogFn {
    // SAFETY: capture ctx.ui ONCE at call time (the getter asserts staleness; the
    // OBJECT does not) — a returned logger may outlive the ctx (async paths,
    // deferred attaches) and must degrade to stderr, never crash pi 1.0.
    const ctxUi = ctx.ui
    const hasUi = Boolean(ctx.hasUI && ctxUi)
    return (level, message) => {
        const line = `mcp-combiner: ${message}`
        // Pi has no structured plugin log sink like OpenCode's client.app.log; surface
        // through the UI when there is one (and the user has not opted out), else stderr.
        if (notify && hasUi && ctxUi?.notify) {
            let uiLevel: "info" | "warn" | "error" = "info"
            if (level === "warn") uiLevel = "warn"
            else if (level === "error") uiLevel = "error"
            try {
                ctxUi.notify(line, uiLevel)
            } catch {
                process.stderr.write(`${line}\n`)
            }
        } else if (level === "error" || level === "warn") {
            process.stderr.write(`${line}\n`)
        }
    }
}
