// /mcp-combiner control verbs: inspect and drive the combiner from a Pi session.
//
// The read side hits the combiner's control plane directly (GET /health, token
// filter routes); the write side drives the combiner__* meta-tools through our own
// MCP client — one auth path, no second protocol.

import type { CombinerConnection } from "./types.js"

export type HealthServer = { name?: string; status?: string; state?: string; transport?: string }

export type HealthSnapshot = {
    servers?: Array<Record<string, unknown>>
    tool_count?: number
    [k: string]: unknown
}

const GLYPHS: Record<string, string> = {
    ready: "✓",
    connected: "✓",
    starting: "…",
    connecting: "…",
    failed: "✗",
    error: "✗",
    disabled: "–",
    idle: "○",
}

function glyph(state: string | undefined): string {
    if (!state) return "?"
    return GLYPHS[state.toLowerCase()] ?? "?"
}

/** `status` verb: health glyph table + our session's tool view. */
export async function statusText(connection: CombinerConnection, cwd: string): Promise<string> {
    const lines: string[] = []
    lines.push(`connection: ${connection.state}${connection.lastError ? ` (${connection.lastError})` : ""}`)
    try {
        const health = (await connection.health()) as HealthSnapshot
        // The combiner ships `servers` as an object keyed by name; tolerate arrays too.
        const raw = health.servers
        const entries: Array<[string, Record<string, unknown>]> = []
        if (Array.isArray(raw)) {
            for (const s of raw) {
                if (typeof s === "object" && s !== null) {
                    const rec = s as Record<string, unknown>
                    entries.push([typeof rec.name === "string" ? rec.name : "?", rec])
                }
            }
        } else if (typeof raw === "object" && raw !== null) {
            for (const [name, s] of Object.entries(raw as Record<string, unknown>)) {
                if (typeof s === "object" && s !== null) entries.push([name, s as Record<string, unknown>])
            }
        }
        for (const [name, s] of entries) {
            const state = typeof (s.status ?? s.state) === "string" ? String(s.status ?? s.state) : undefined
            const transport = typeof s.transport === "string" ? ` [${s.transport}]` : ""
            const dis = s.disabled === true ? " (disabled)" : ""
            lines.push(`${glyph(state)} ${name}${transport} (${state ?? "unknown"})${dis}`)
        }
    } catch (e) {
        lines.push(`/health unreachable: ${e instanceof Error ? e.message : String(e)}`)
    }
    try {
        const tools = await connection.listTools()
        const servers = [...new Set(tools.map((t) => t.name.split("_", 1)[0] ?? ""))].filter(Boolean).sort()
        lines.push(
            `visible to this session: ${tools.length} tools across ${servers.length} servers (${servers.join(", ")})`,
        )
    } catch {
        // connection state already reported above
    }
    lines.push(`cwd: ${cwd}`)
    return lines.join("\n")
}

/** `enable` / `disable` / `restart-server` verbs via the combiner meta-tools. */
export async function metaToolCall(
    connection: CombinerConnection,
    verb: "enable" | "disable" | "restart-server",
    server: string,
): Promise<string> {
    const map: Record<typeof verb, string> = {
        enable: "combiner__enable_server",
        disable: "combiner__disable_server",
        "restart-server": "combiner__restart_server",
    }
    const tool = map[verb]
    const result = (await connection.callTool(tool, { server_name: server })) as { content?: Array<{ text?: string }> }
    const text = (result?.content ?? [])
        .map((b) => b.text ?? "")
        .join("\n")
        .trim()
    return text || `${verb} ${server}: done`
}

// ── per-chat collections ────────────────────────────────────────

export type CollectionAction = "on" | "off" | "reset"

type CollectionServer = { running?: boolean; this_chat?: boolean }
type CollectionEntry = {
    enabled?: boolean
    this_chat?: boolean
    this_chat_set?: boolean
    servers?: Record<string, CollectionServer>
}

function textOf(result: unknown): string {
    const blocks = (result as { content?: Array<{ text?: string }> } | undefined)?.content ?? []
    return blocks
        .map((b) => b.text ?? "")
        .join("\n")
        .trim()
}

function chatArgs(connection: CombinerConnection): Record<string, unknown> {
    // The combiner resolves this chat from its own token; naming it too keeps
    // the call right even when the session isn't bound to the token yet.
    return connection.sessionToken ? { chat_id: connection.sessionToken } : {}
}

/** `collections` verb: each collection, on or off for this chat and for the
 *  combiner, and its servers (running? visible here?). */
export async function collectionsText(connection: CombinerConnection): Promise<string> {
    const raw = textOf(await connection.callTool("combiner__collections", chatArgs(connection)))
    let parsed: Record<string, CollectionEntry>
    try {
        parsed = JSON.parse(raw) as Record<string, CollectionEntry>
    } catch {
        return raw || "collections: no answer"
    }
    const names = Object.keys(parsed).sort()
    if (names.length === 0) return "No collections are configured on this machine."
    const lines: string[] = []
    for (const name of names) {
        const c = parsed[name] ?? {}
        const here = c.this_chat ? "on" : "off"
        const why = c.this_chat_set ? "set for this chat" : "following the combiner"
        lines.push(`${c.this_chat ? "●" : "○"} ${name}: ${here} here (${why}; combiner: ${c.enabled ? "on" : "off"})`)
        for (const [srv, s] of Object.entries(c.servers ?? {}).sort(([a], [b]) => a.localeCompare(b))) {
            const state = s.this_chat ? "visible here" : s.running ? "running, hidden here" : "not running"
            lines.push(`    ${srv} — ${state}`)
        }
    }
    lines.push("Change for this chat: /mcp-combiner collection on|off|reset <name>")
    return lines.join("\n")
}

/** `collection on|off|reset <name>` verb: one collection for this chat only. */
export async function collectionCall(
    connection: CombinerConnection,
    action: CollectionAction,
    name: string,
): Promise<string> {
    const tool = {
        on: "combiner__session_enable_collection",
        off: "combiner__session_disable_collection",
        reset: "combiner__session_reset_collection",
    }[action]
    const raw = textOf(await connection.callTool(tool, { collection: name, ...chatArgs(connection) }))
    let said: {
        this_chat?: boolean
        this_chat_set?: boolean
        servers?: Record<string, boolean>
        started?: string[]
        stopped?: string[]
        failed?: string[]
    }
    try {
        said = JSON.parse(raw) as typeof said
    } catch {
        return raw || `collection ${action} ${name}: done` // e.g. "Error: collection 'x' isn't loaded here"
    }
    const state = action === "reset" ? `follows the combiner (${said.this_chat ? "on" : "off"})` : said.this_chat ? "on" : "off"
    const parts = [`Collection '${name}' ${state} for this chat`]
    const visible = Object.entries(said.servers ?? {})
        .filter(([, seen]) => seen)
        .map(([srv]) => srv)
    if (visible.length) parts.push(`visible here: ${visible.sort().join(", ")}`)
    if (said.started?.length) parts.push(`started ${said.started.join(", ")}`)
    if (said.stopped?.length) parts.push(`stopped ${said.stopped.join(", ")}`)
    if (said.failed?.length) parts.push(`failed: ${said.failed.join(", ")}`)
    return `${parts.join("; ")}.`
}
