// Footer status line for enabled MCP servers, compatible with pi-mcp-adapter's
// mcpFooterStatus setting: "full" (default) = "N servers enabled (M ready) · T tools",
// "compact" = "MCP M/N", "off" = none. Published via ctx.ui.setStatus under the key
// `mcp` (default, overridable via the mcpFooterKey setting) — the slot
// pi-mcp-adapter wrote and oh-my-posh-style footers aggregate into PI_STATUS.

import type { CombinerConnection } from "./types.js"
import type { ExtensionUIContext } from "../pi.js"
import type { FooterStatus } from "./settings.js"

/** Health entry server states we count as "ready" (combiner /health semantics). */
const READY_STATES = new Set(["ready", "connected"])

/** nf-fa-plug (U+F1E6), emitted directly — a nerd-font codepoint, never an emoji,
 *  so the glyph renders in any nerd-font terminal and passes through footer
 *  translation layers (oh-my-posh's ICON_MAP) untouched. */
const PLUG = "\uF1E6"

/** A /health `servers` entry — the combiner ships an OBJECT keyed by server name. */
type HealthServer = { state?: string; status?: string; disabled?: boolean }

export type FooterCounts = { enabled: number; ready: number; disabled: number }

/** Derive enabled/ready/disabled counts from a /health snapshot. Tolerates both
 *  the object-keyed shape (what the combiner serves) and an array (defensive). */
export function countsFromHealth(health: unknown): FooterCounts {
    const raw = (health as { servers?: unknown })?.servers
    const entries: Array<[string, HealthServer]> = []
    if (Array.isArray(raw)) {
        for (const s of raw) if (typeof s === "object" && s !== null) entries.push(["", s as HealthServer])
    } else if (typeof raw === "object" && raw !== null) {
        for (const [name, s] of Object.entries(raw as Record<string, unknown>)) {
            if (typeof s === "object" && s !== null) entries.push([name, s as HealthServer])
        }
    } else {
        return { enabled: 0, ready: 0, disabled: 0 }
    }
    let enabled = 0
    let ready = 0
    let disabled = 0
    for (const [, s] of entries) {
        if (s.disabled) {
            disabled++
            continue
        }
        enabled++
        const state = (s.state ?? s.status ?? "").toLowerCase()
        if (READY_STATES.has(state)) ready++
    }
    return { enabled, ready, disabled }
}

/** A native tool-registration pass in progress: tools registered so far / to register. */
export type RegisterProgress = { done: number; total: number }

/** Render the footer text for a mode (undefined = clear the slot). */
export function footerText(
    mode: FooterStatus,
    counts: FooterCounts,
    connectionState: string,
    toolCount?: number,
    registering?: RegisterProgress,
): string | undefined {
    if (mode === "off") return undefined
    if (mode === "compact") return `${PLUG} MCP ${counts.ready}/${counts.enabled}`
    let text = `${PLUG} ${counts.enabled} ${counts.enabled === 1 ? "server" : "servers"} enabled`
    if (counts.ready > 0) text += ` (${counts.ready} ready)`
    if (counts.disabled > 0) text += ` (${counts.disabled} disabled)`
    if (registering && registering.done < registering.total) {
        text += ` · registering tools ${registering.done}/${registering.total}`
    } else if (typeof toolCount === "number" && toolCount > 0) text += ` · ${toolCount} tools`
    if (connectionState !== "connected" && connectionState !== "disconnected") {
        text += ` — ${connectionState}`
    }
    return text
}

let warnedNoSetStatus = false

/** Refresh the footer from live health + this session's tool view. Never throws;
 *  logs (once) when the UI cannot take a status so a dead label is diagnosable. */
export async function updateFooter(
    connection: CombinerConnection,
    ui: ExtensionUIContext | undefined,
    key: string,
    mode: FooterStatus,
    /** Read AFTER the /health and tools/list awaits, so a slow paint that resolves
     *  last shows the progress as it is then, not as it was when it started. */
    registering?: () => RegisterProgress | undefined,
): Promise<void> {
    if (mode === "off") return
    const setStatus = ui?.setStatus
    if (!setStatus) {
        if (!warnedNoSetStatus) {
            warnedNoSetStatus = true
            // Headless/print modes have no footer — fine — but a TUI session with no
            // setStatus deserves one log line rather than a silently missing label.
            if (ui) console.error(`mcp-combiner: footer unavailable (ctx.ui has no setStatus)`)
        }
        return
    }
    let text: string | undefined
    try {
        // List tools only on a live connection: a footer repaint must never start a
        // connect itself (it would bypass the connection's reconnect backoff).
        const tools = connection.state === "connected" ? connection.listTools().catch(() => undefined) : undefined
        const [health, toolList] = await Promise.all([connection.health(), tools])
        const toolCount = toolList?.length
        text = footerText(mode, countsFromHealth(health), connection.state, toolCount, registering?.())
    } catch {
        // Combiner unreachable: report our connection state honestly, still occupying
        // the slot so the label never silently vanishes.
        text = footerText(mode, { enabled: 0, ready: 0, disabled: 0 }, connection.state)
    }
    try {
        setStatus(key, text)
    } catch (e) {
        console.error(`mcp-combiner: footer setStatus failed (${e instanceof Error ? e.message : String(e)})`)
    }
}
