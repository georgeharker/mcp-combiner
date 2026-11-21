// NATIVE MODE : activation entry for the
// pi-mcp-based combiner connection + native tool surface. This file exists so the
// wire-in stays a few lines in src/index.ts:
//
//   const conn = new NativeCombinerConnection(…, clientLog)   // implements CombinerConnection
//   const nativeSurface = activateNativeMode(pi, { connection: conn, … })
//   … nativeSurface.run() at session_start (post-setToken) and on list_changed
//
// The FIRST run is deferred by design (see activateNativeMode): the grouping token
// only exists after session_start. Configuration (directTools spec, server filter)
// rides as GETTERS so every pass resolves the CURRENT session's values — worktree
// and project switches re-resolve session config, and a load-time snapshot would
// apply the wrong allow/deny set to later sessions (review finding #5).

import type { ExtensionAPI, ExtensionContext } from "../pi.js"
import type { ServerFilter } from "../client/config-ladder.js"
import { NativeCombinerConnection } from "./combiner-connection.js"
import { activateNativeTools, type NativeToolSurfaceState } from "./tool-surface.js"
import { activateNativeResources } from "./resources.js"

export { NativeCombinerConnection } from "./combiner-connection.js"
export { activateNativeTools, type NativeToolSurfaceOptions, type NativeToolSurfaceState } from "./tool-surface.js"
export { tokenedUrl } from "./combiner-connection.js"
export { activateNativeResources, filterInteractiveResources, type NativeResourceOptions } from "./resources.js"

export type NativeActivationOptions = {
    connection: NativeCombinerConnection
    /** Per-project directTools spec, resolved PER PASS (not a load-time snapshot). */
    directSpec?: () => string[] | "search" | undefined
    /** Per-project server filter, resolved PER PASS (same reason). */
    serverFilter?: () => ServerFilter | undefined
    /** Register the interactive read_<resource> tools (index.ts's exposeResources
     *  setting); default true. */
    exposeResources?: boolean
    /** Auto-open widget URLs (index.ts's uiAutoOpen setting); default true. */
    uiAutoOpen?: boolean
    /** Warn when the direct-exposure set grows large; settings kill-switch
     *  warnLargeDirectExposure disables it (default true, i.e. warn). */
    warnLargeDirectExposure?: boolean
    /** Cap on guarded tool-result text before spill-to-file (maxResultChars setting);
     *  forwarded to the tool surface and the interactive read_* tools. */
    maxResultChars?: number
    /** Tool-registration progress for the current pass (registered, total); see
     *  NativeToolSurfaceOptions.onProgress. index.ts drives the footer from it. */
    onToolProgress?: (done: number, total: number) => void
    log: (level: "info" | "warn" | "error", message: string) => void
}

/** The native surface's lifecycle, as returned by activateNativeMode. */
export type NativeSurface = {
    /** Start a registration pass against the current catalog (supersedes one in flight). */
    run(): void
    /** True while a tool registration pass is running (connecting, listing, or
     *  registering). */
    busy(): boolean
    /** Resolves when no tool pass is running: the newest pass has ended, however it
     *  ended (finished, failed, stopped). Never rejects. */
    settled(): Promise<void>
    /** Stop: in-flight passes end at their next check; run() becomes a no-op. */
    dispose(): void
}

/** How long a turn waits for tool registration before starting with what it has.
 *  Covers the worst cold start: the connect retry (15 s budget) plus a full
 *  registration pass (~10 s for ~1,000 tools). */
export const TURN_WAIT_MS = 30_000

/** Wait until `surface` has no registration pass running, at most `timeoutMs`, or
 *  until `signal` aborts or `onInterrupt` fires (it receives a callback that ends
 *  the wait and returns an unsubscribe; index.ts wires it to Esc / Ctrl+C / Ctrl+D,
 *  since pi has no abort signal at before_agent_start). Never rejects; the timer is
 *  cleared (and unref'd) and the listeners removed, so a wait that ends early never
 *  holds pi open. */
export async function waitForRegistration(
    surface: Pick<NativeSurface, "busy" | "settled">,
    opts: { timeoutMs?: number; signal?: AbortSignal; onInterrupt?: (interrupt: () => void) => () => void } = {},
): Promise<"idle" | "settled" | "timeout" | "aborted"> {
    if (!surface.busy()) return "idle"
    if (opts.signal?.aborted) return "aborted"
    let timer: ReturnType<typeof setTimeout> | undefined
    let onAbort: (() => void) | undefined
    let unsubscribe: (() => void) | undefined
    try {
        return await Promise.race([
            surface.settled().then(() => "settled" as const),
            new Promise<"timeout">((resolve) => {
                timer = setTimeout(() => resolve("timeout"), opts.timeoutMs ?? TURN_WAIT_MS)
                timer.unref?.()
            }),
            new Promise<"aborted">((resolve) => {
                onAbort = () => resolve("aborted")
                opts.signal?.addEventListener("abort", onAbort, { once: true })
                try {
                    unsubscribe = opts.onInterrupt?.(onAbort)
                } catch {
                    // an interrupt source that fails just isn't one
                }
            }),
        ])
    } finally {
        if (timer) clearTimeout(timer)
        if (onAbort) opts.signal?.removeEventListener("abort", onAbort)
        unsubscribe?.()
    }
}

/** Register the declared tool surface + interactive-resource read_* tools + the
 *  list_changed re-registration handler. Called once at session_start; the
 *  list_changed hook re-syncs both surfaces on the same names. One shared
 *  NativeToolSurfaceState diff across all passes — unchanged declarations are
 *  skipped, removed ones re-registered hidden (see activateNativeTools). */
export function activateNativeMode(
    pi: ExtensionAPI,
    opts: NativeActivationOptions,
): NativeSurface {
    let active = true
    const toolState: NativeToolSurfaceState = { names: new Set(), signatures: new Map() }
    // Cross-pass memory for the read_* tools (skip re-registration of unchanged
    // names — pi.registerTool replaces-by-name with a warning).
    const resourceRegistered = new Set<string>()
    // Only the NEWEST tool pass may change toolState. activateNativeTools yields
    // between registration slices, so passes can overlap in time; each run() bumps
    // `generation`, and a pass whose generation is stale stops at its next check.
    // That is enough on its own: a pass checks shouldContinue() after every await
    // (listTools and each slice yield), and every state change happens synchronously
    // between those checks, so a superseded pass never touches state again.
    // index.ts calls dispose() at session_shutdown and on reload teardown, which
    // stops a pass the same way before pi retires the session's extension API.
    let generation = 0
    // The newest tool pass, until it ends; undefined when no pass is running. A
    // superseded pass doesn't count: settled() follows whichever pass is newest.
    let latest: Promise<void> | undefined
    const run = () => {
        if (!active) return
        const mine = ++generation
        const current = () => active && generation === mine
        // A pass that fails after this surface was disposed or superseded (pi's
        // registerTool throws once the session's extension API is retired) is not
        // news: only the newest live pass reports failures.
        const failed = (what: string) => (e: unknown) => {
            if (current()) opts.log("warn", `${what} failed: ${e instanceof Error ? e.message : String(e)}`)
        }
        const tools = activateNativeTools(pi, {
            connection: opts.connection,
            serverFilter: opts.serverFilter,
            directSpec: opts.directSpec,
            state: toolState,
            warnLargeDirectExposure: opts.warnLargeDirectExposure,
            maxResultChars: opts.maxResultChars,
            shouldContinue: current,
            onProgress: opts.onToolProgress,
            log: opts.log,
        }).then(() => undefined, failed("native tool surface"))
        const resources =
            opts.exposeResources !== false
                ? activateNativeResources(
                      pi,
                      {
                          connection: opts.connection,
                          serverFilter: opts.serverFilter,
                          directSpec: opts.directSpec,
                          uiAutoOpen: opts.uiAutoOpen,
                          maxResultChars: opts.maxResultChars,
                          shouldContinue: current,
                          log: opts.log,
                      },
                      resourceRegistered,
                  ).then(() => undefined, failed("native resource surface"))
                : Promise.resolve()
        // The pass is both surfaces: a turn that waits on settled() gets the read_*
        // tools too. The newest pass always ends with a "done" report, even when it
        // had nothing to register or failed, so a "registering" left by a superseded
        // pass never sticks in the footer.
        const pass: Promise<void> = Promise.all([tools, resources]).then(
            () => {
                // Clear first: a throwing progress hook must not leave busy() true.
                if (latest === pass) latest = undefined
                if (current()) opts.onToolProgress?.(0, 0)
            },
        )
        latest = pass
    }
    // The FIRST run is the caller's to schedule: the grouping token only exists
    // after session_start (the connection refuses to connect without it), so a
    // factory-time run() would fail with "no grouping token set" — the startup
    // warnings this signature used to emit. index.ts calls run() at
    // session_start (post-setToken) and from its own onToolsChanged hook; we
    // deliberately do NOT setHooks here — a second setHooks call from the
    // wiring would replace ours wholesale (last-wins), so the re-sync lives in
    // ONE hook set, index.ts's.
    return {
        run,
        busy: () => latest !== undefined,
        async settled() {
            // A pass that ends because a newer one superseded it hands over: keep
            // waiting until no pass is running.
            while (latest) await latest
        },
        dispose() {
            active = false
        },
    }
}

// NOTE on codemode activation (verified live, pi 1.0.x): pi does NOT
// auto-activate the codemode tool for extension-registered codeme tools (only
// for connected MCP servers) — enable it with settings.json →
//   "defaultTools": ["+codemode"]
// (defaultTools applies at a FRESH session's creation; resumed sessions keep
// their recorded tool set). With codemode on, our registered tools are
// first-class script members under their bare names (`tools.mock_echo(...)`),
// searchTools() discovers them (embedding their TS declarations), and
// pi's codemode globals are text()/image()/console.log/return (no emit).
// Also verified: registerTool accepts exposure/namespace/annotations, and
// the peer-dep import resolves from the host's installed package tree.
export type { ExtensionContext }