// The combiner PROCESS, as this pi process sees it: launching it or attaching to it
// through sharedserver, holding this pi process's reference on it, checking it's
// alive, respawning it (rate-capped), and releasing the reference at quit. One place
// for all of it, so there is one launch procedure.
//
// Rules (design: pi-one-combiner-process-module-one-attach-function-for-launch-ref-and-respawn-respawn-capped-at-2-per-minute.md):
//   - attach() is the ONLY way pi launches the combiner or takes a ref
//     (`sharedserver use`). Every pi process holds its own ref: sharedserver keys
//     refs by pid, and several pi processes are normally attached at once.
//   - After a reconnect, if the combiner is a DIFFERENT process from the one this pi
//     attached to (someone else started or restarted it, which drops every client's
//     ref), call attach() again: same function, same ref.
//   - When connects are refused, the connection asks onRefused(): our process still
//     alive -> keep retrying; a different process -> attach() and keep retrying;
//     dead -> respawn with attach(), at most RESPAWN_LIMIT times per
//     RESPAWN_WINDOW_MS. One more inside the window stops respawning and reports it.
//   - Host-owned / unmanaged combiners never get a CombinerProcess (index.ts).
//
// State is per pi PROCESS (globalThis, Symbol.for): pi re-instantiates the extension
// for every session (jiti moduleCache:false), but there is one ref per pid.

import { spawn, spawnSync } from "node:child_process"
import { existsSync, mkdirSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { resolveSharedserver } from "./sharedserver-resolve.js"
import type { Liveness } from "./native/combiner-connection.js"

export type LogFn = (level: "info" | "warn" | "error", message: string) => void

export const DEFAULT_PORT = 9741
const DEFAULT_NAME = "mcp-combiner"
const DEFAULT_GRACE = "30m"
// The `--mcp` serve flag arrived in combiner 0.8.0; older versions serve with a bare
// `--config`. That boundary is also the floor for trusting a PATH install — below it we
// fetch a known-good release via uvx rather than limp along. NOT the lockstep release
// version; it moves only when this extension depends on newer combiner behaviour.
const MIN_COMBINER_VERSION: [number, number, number] = [0, 8, 0]
// Floor-only against sharedserver's latest release: this repo consumes sharedserver
// rather than shipping it. Kept equal to the OpenCode plugin's and the Claude plugin's
// (bin/sharedserver.conf) values: they resolve the same binary the same way.
// 0.9.6 adds the launch lock: concurrent `use` calls on a stopped server start exactly
// one and keep every ref (0.9.5 could start two and lose refs). The automatic respawn
// in this module relies on it: several pi processes respawn a dead combiner at once.
const SHAREDSERVER_MIN_VERSION = "0.9.6"


/** Respawn cap: at most RESPAWN_LIMIT respawns in any RESPAWN_WINDOW_MS; one more
 *  inside the window means the combiner keeps dying, so stop and report. */
export const RESPAWN_LIMIT = 2
export const RESPAWN_WINDOW_MS = 60_000
/** How often watch() checks that the combiner process still exists. */
export const WATCH_INTERVAL_MS = 2_000
/** Every this many watch ticks (~30 s), also re-read sharedserver's view: catches a
 *  reused pid and a different combiner process replacing ours. */
export const WATCH_STATUS_EVERY = 15
/** How long after this pi launched the combiner it counts as still booting (the
 *  connection's first-connect budget). */
export const COLD_START_MS = 15_000

/** `sharedserver use` attempts while the server reads "defunct" (being reaped). */
const ATTACH_DEFUNCT_RETRIES = 4
const ATTACH_DEFUNCT_DELAY_MS = 300

// ── env configuration ──────────────────────────────────────────────

export function env(name: string): string | undefined {
    const v = process.env[name]
    return v !== undefined && v !== "" ? v : undefined
}

function splitArgs(value: string | undefined): string[] {
    if (!value) return []
    return value.split(/\s+/).filter((s) => s.length > 0)
}

// ── combiner command resolution (ported from the OpenCode plugin) ──

type Command = {
    cmd: string
    /** Structural args identifying WHAT to run. Probed with; never omitted. */
    args: string[]
    /** The user's own extra args. Appended at spawn time only — never probed with. */
    extra?: string[]
    version?: [number, number, number]
}

function parseVersion(text: string): [number, number, number] | undefined {
    const m = text.match(/(\d+)\.(\d+)\.(\d+)/)
    if (!m) return undefined
    return [Number(m[1]), Number(m[2]), Number(m[3])]
}

function gte(a: [number, number, number], b: [number, number, number]): boolean {
    for (let i = 0; i < 3; i++) {
        if (a[i] !== b[i]) return a[i] > b[i]
    }
    return true
}

/** Probe a command once for BOTH facts: is it runnable, and which version. Presence is
 *  "spawn did not fail", NOT "exited 0" — a pre-0.8.0 combiner exits non-zero on
 *  `--version` (the flag did not exist), which is exactly the stale install the floor
 *  exists to catch. Probes with ONLY the structural args; the user's extras are never
 *  folded in, so one arg the CLI rejects cannot make a healthy install look absent. */
function probe(cmd: Command): { present: boolean; version?: [number, number, number] } {
    const r = spawnSync(cmd.cmd, [...cmd.args, "--version"], { env: process.env })
    if (r.error) return { present: false }
    if (r.status !== 0) return { present: true }
    return { present: true, version: parseVersion(`${r.stdout?.toString() ?? ""}${r.stderr?.toString() ?? ""}`) }
}

function probeUsable(cmd: Command): Command | undefined {
    const { version } = probe(cmd)
    if (!version || !gte(version, MIN_COMBINER_VERSION)) return undefined
    return { ...cmd, version }
}

/** This extension's own version, kept equal to the published PyPI mcp-combiner by
 *  lockstep releases (scripts/bump-version.sh). Used as the uvx pin — read from the
 *  manifest, never duplicated as a constant. */
const PLUGIN_VERSION: string | undefined = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8")) as { version?: string }
        return pkg.version
    } catch {
        return undefined
    }
})()

/** Resolve how to invoke the combiner, mirroring the sibling plugins' priority:
 *  explicit env command → `mcp-combiner` on PATH (only when new enough) →
 *  `uv run --project <checkout> python -m mcp_combiner` → a pinned release via uvx. The
 *  uvx tail is what makes a bare install work with nothing installed by hand. */
function resolveCombiner(log: LogFn): Command | undefined {
    const extra = splitArgs(env("PI_MCP_COMBINER_ARGS"))

    const command = env("PI_MCP_COMBINER_COMMAND")
    if (command) {
        return { cmd: command, args: [], extra }
    }

    const onPath: Command = { cmd: "mcp-combiner", args: [] }
    const pathProbe = probe(onPath)
    if (pathProbe.present) {
        if (pathProbe.version && gte(pathProbe.version, MIN_COMBINER_VERSION)) {
            return { ...onPath, extra, version: pathProbe.version }
        }
        log(
            "warn",
            `mcp-combiner on PATH reports ${pathProbe.version?.join(".") ?? "a pre-0.8.0 version"}, older than ` +
                `${MIN_COMBINER_VERSION.join(".")} — ignoring it and fetching a pinned release instead. ` +
                "Upgrade with: uv tool install --upgrade mcp-combiner",
        )
    }

    const checkout = env("PI_MCP_COMBINER_CHECKOUT")
    if (checkout && existsSync(checkout) && probe({ cmd: "uv", args: [] }).present) {
        return { cmd: "uv", args: ["run", "--project", checkout, "python", "-m", "mcp_combiner"], extra }
    }

    // Out-of-the-box path: no install required, fetch from PyPI. Pinned to this
    // extension's version so the pair moves in lockstep; if that exact release is
    // missing, fall back to latest rather than failing. The probe both validates the pin
    // and warms uv's cache, so the real spawn is a cache hit.
    if (probe({ cmd: "uvx", args: [] }).present) {
        if (PLUGIN_VERSION) {
            const pinned = probeUsable({ cmd: "uvx", args: [`mcp-combiner@${PLUGIN_VERSION}`] })
            if (pinned) return { ...pinned, extra }
            log("warn", `pinned mcp-combiner@${PLUGIN_VERSION} unavailable or too old; trying latest from PyPI`)
        }
        const latest = probeUsable({ cmd: "uvx", args: ["mcp-combiner"] })
        if (latest) return { ...latest, extra }
    }

    return undefined
}

/** True if the resolved combiner supports (needs) the `--mcp` serve flag. Unknown
 *  version → assume NO: a version we cannot read is a pre-0.8.0 build serving on a bare
 *  `--config`, and a bare `--config` still serves on newer releases (with a deprecation
 *  warning), so guessing low degrades gracefully whereas guessing high does not. */
function combinerNeedsMcpFlag(cmd: Command): boolean {
    const ver = cmd.version ?? probe({ cmd: cmd.cmd, args: cmd.args }).version
    return ver ? gte(ver, MIN_COMBINER_VERSION) : false
}

// ── servers.json resolution (mirrors the sibling plugins) ──

function resolveConfig(log: LogFn): string | undefined {
    const explicit = env("PI_MCP_COMBINER_CONFIG")
    if (explicit) return explicit
    const user = process.env.USER ?? process.env.LOGNAME ?? ""
    const candidates = [
        join(homedir(), ".cache", "secrets", `${user}.mcpservers.json`),
        join(homedir(), ".config", "mcp-combiner", "servers.json"),
        join(homedir(), ".config", "mcp", "servers.json"),
    ]
    const found = candidates.find(existsSync)
    if (!found) {
        log(
            "error",
            "no combiner servers.json found; set $PI_MCP_COMBINER_CONFIG (probed " +
                "~/.cache/secrets/<user>.mcpservers.json, ~/.config/mcp-combiner/servers.json, " +
                "~/.config/mcp/servers.json)",
        )
    }
    return found
}

// ── which combiner process ─────────────────────────────────────────

/** A combiner process: its pid and start time, so a reused pid isn't mistaken for
 *  the same process. */
export type CombinerIdentity = { pid: number; startTime: number | undefined }

/** What sharedserver says about `name` (`sharedserver info <name> --json`):
 *  - running: active or in its grace period, with the real server process;
 *  - stopped: not running, or defunct (it died; sharedserver is cleaning up);
 *  - unknown: a start is still publishing its process (`watcher_pid` null), or the
 *    lookup got no answer (`noAnswer`: timed out, errored, or unparseable output).
 *  sharedserver answers in a few ms with a definite state, so no answer is a
 *  sharedserver bug: callers report it and fall back to checking the pid. */
export type CombinerStatus =
    | { kind: "running"; identity: CombinerIdentity }
    | { kind: "stopped" }
    | { kind: "unknown"; noAnswer?: string }

/** Cap on the synchronous `sharedserver use` / `unuse` (they normally take a few
 *  ms; a hung sharedserver must not freeze pi's main thread indefinitely). */
const SHAREDSERVER_TIMEOUT_MS = 10_000

/** How long to wait for `sharedserver info` (it normally answers in ~4 ms). */
export const STATUS_TIMEOUT_MS = 500

/** Parse `sharedserver info --json` output (exported for tests). */
export function parseCombinerStatus(out: string): CombinerStatus {
    let info: { state?: unknown; pid?: unknown; start_time?: unknown; watcher_pid?: unknown }
    try {
        info = JSON.parse(out) as typeof info
    } catch {
        return { kind: "unknown", noAnswer: "unparseable output" }
    }
    if (info.state === "stopped" || info.state === "defunct") return { kind: "stopped" }
    if (info.state !== "active" && info.state !== "grace") return { kind: "unknown" }
    if (typeof info.pid !== "number" || typeof info.watcher_pid !== "number") return { kind: "unknown" }
    return {
        kind: "running",
        identity: { pid: info.pid, startTime: typeof info.start_time === "number" ? info.start_time : undefined },
    }
}

/** Ask sharedserver about `name`. Async (never blocks pi's main thread). */
function combinerStatus(binary: string, name: string): Promise<CombinerStatus> {
    return new Promise((resolve) => {
        const child = spawn(binary, ["info", name, "--json"], { stdio: ["ignore", "pipe", "ignore"], env: process.env })
        child.unref()
        let out = ""
        child.stdout?.on("data", (chunk: Buffer) => {
            out += chunk.toString()
        })
        const timer = setTimeout(() => {
            child.kill()
            resolve({ kind: "unknown", noAnswer: `no answer within ${STATUS_TIMEOUT_MS} ms` })
        }, STATUS_TIMEOUT_MS)
        timer.unref()
        child.on("error", (e) => {
            clearTimeout(timer)
            resolve({ kind: "unknown", noAnswer: `couldn't run it (${e.message})` })
        })
        // "close", not "exit": stdout is only fully read by then.
        child.on("close", (code) => {
            clearTimeout(timer)
            resolve(code === 0 ? parseCombinerStatus(out) : { kind: "unknown", noAnswer: `it exited ${code}` })
        })
    })
}

function sameCombiner(a: CombinerIdentity | undefined, b: CombinerIdentity): boolean {
    return a !== undefined && a.pid === b.pid && a.startTime === b.startTime
}

function identityKey(id: CombinerIdentity | undefined): string {
    return id ? `${id.pid}:${id.startTime ?? "?"}` : "unknown"
}

// ── port ───────────────────────────────────────────────────────────

/** The port to serve on, reconciling PI_MCP_COMBINER_PORT with any explicit
 *  MCP_COMPANION_COMBINER_URL. */
export function resolvePort(log: LogFn): number {
    const raw = env("PI_MCP_COMBINER_PORT")
    let port = DEFAULT_PORT
    if (raw !== undefined) {
        const n = Number(raw)
        if (Number.isInteger(n) && n > 0) port = n
        else log("warn", `PI_MCP_COMBINER_PORT=${raw} is not a positive integer; using ${DEFAULT_PORT}`)
    }
    const url = env("MCP_COMPANION_COMBINER_URL")
    if (url && raw !== undefined) {
        let urlPort = Number.NaN
        try {
            urlPort = Number(new URL(url).port)
        } catch {
            // malformed URL — fall through to the no-usable-port warning below
        }
        if (!Number.isInteger(urlPort) || urlPort <= 0) {
            log(
                "warn",
                `MCP_COMPANION_COMBINER_URL=${url} has no explicit port; serving on ${port}. ` +
                    `Use an explicit port in the URL (e.g. http://127.0.0.1:${port}/mcp).`,
            )
        } else if (urlPort !== port) {
            log(
                "warn",
                `PI_MCP_COMBINER_PORT=${port} disagrees with MCP_COMPANION_COMBINER_URL=${url}; the URL is what ` +
                    `pi-mcp-adapter registers, so serving on ${port} would be unreachable — using ${urlPort}. ` +
                    "Set them to the same port.",
            )
            port = urlPort
        }
    }
    return port
}

// ── per-process state ──────────────────────────────────────────────

/** The combiner process this pi holds its ref on. `identity` is undefined until
 *  known (filled in lazily when the attach-time lookup came back unknown). */
export type Attachment = { binary: string; name: string; identity: CombinerIdentity | undefined }

export type ProcessState = {
    attachment: Attachment | null
    /** Set at quit: an attach in flight must not re-take the combiner afterwards. */
    quitting: boolean
    /** attach()'s single flight, so concurrent callers share one `use`; and whether
     *  that flight may launch (a launching caller never settles for an attach-only
     *  flight that failed). */
    attaching?: Promise<boolean>
    attachingLaunches?: boolean
    /** The respawn in flight, keyed by the dead process: concurrent callers for the
     *  same death share it, so one death counts once. */
    respawning?: { key: string; promise: Promise<Liveness> }
    /** When this pi respawned a dead combiner (ms timestamps, newest last). */
    respawns: number[]
    /** Set once the respawn cap was hit (or restarts kept failing); cleared by start(). */
    respawnStopped: boolean
    /** Consecutive restarts whose attach failed (no combiner launched). */
    respawnFailures: number
    /** When this pi's own `use` launched the combiner (it said "Started server"):
     *  a combiner that young is still booting, so connects get the cold-start budget. */
    launchedAt?: number
    /** How many times this pi's `use` launched the combiner (vs only attached). */
    launches: number
    /** The combiner's log files, for the report when respawning stops. */
    logs: string[]
}

/** The globalThis key of the per-process state (tests reset it). */
export const COMBINER_PROCESS_STATE = Symbol.for("mcp-companion:combiner-process")
const STATE = COMBINER_PROCESS_STATE
function processState(): ProcessState {
    const g = globalThis as Record<symbol, ProcessState | undefined>
    return (g[STATE] ??= { attachment: null, quitting: false, respawns: [], respawnStopped: false, respawnFailures: 0, launches: 0, logs: [] })
}

/** Process-global guard: the exit and signal handlers are installed once per
 *  process no matter how many sessions bind (same trick pi-permission-system's
 *  service.ts uses). */
const CLEANUP_GUARD = Symbol.for("mcp-companion:cleanup-installed")

function installProcessCleanup(): void {
    const g = globalThis as Record<symbol, boolean | undefined>
    if (g[CLEANUP_GUARD]) return
    g[CLEANUP_GUARD] = true
    // Belt to the session_shutdown("quit") braces: if Pi is killed hard enough that
    // session_shutdown never fires, still release the refcount. Idempotent via `detach`.
    process.on("exit", () => detach())
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        const handler = () => {
            detach()
            // Remove our listener before re-raising: re-killing with the handler
            // still installed re-delivers the signal to it forever (loop verified —
            // ctrl+c wedged pi instead of exiting). Gone, the re-raise falls through
            // to the default disposition and terminates; the idempotent "exit"
            // belt stays for any path that gets there.
            process.removeListener(sig, handler)
            process.kill(process.pid, sig)
        }
        process.on(sig, handler)
    }
}

/** Release this pi process's ref (sharedserver `unuse`). Idempotent. */
function detach(): void {
    const st = processState()
    if (!st.attachment) return
    const { binary, name } = st.attachment
    st.attachment = null
    spawnSync(binary, ["unuse", name, "--pid", String(process.pid)], {
        stdio: "ignore",
        env: process.env,
        timeout: SHAREDSERVER_TIMEOUT_MS,
    })
}

// ── the class ──────────────────────────────────────────────────────

export class CombinerProcess {
    protected readonly state = processState()
    private log: LogFn
    private releaseReady: () => void = () => undefined
    /** Resolves once this instance's start() has run, whatever its outcome. Connects
     *  wait on it, so the first connect never races the launch. */
    readonly ready: Promise<void>
    /** Set when a connect succeeded with no attachment (start()'s attach failed):
     *  attach once then, not on every reconnect (the attach runs synchronous probes). */
    private reconnectAttachTried = false

    constructor(log: LogFn) {
        this.log = log
        this.ready = new Promise<void>((resolve) => {
            this.releaseReady = resolve
        })
    }

    /** True while this pi process holds a ref on a combiner process. */
    get attached(): boolean {
        return this.state.attachment !== null
    }

    /** session_start: attach (launching if needed) unless this pi process already
     *  holds a ref; always releases `ready`. An explicit start also lifts a respawn
     *  stop and clears the respawn history. */
    async start(log: LogFn): Promise<void> {
        this.log = log
        try {
            // An explicit start (a new session) is the user's say-so: lift a respawn
            // stop and forget earlier respawns.
            this.state.respawnStopped = false
            this.state.respawns = []
            this.state.respawnFailures = 0
            if (!this.state.attachment) await this.attach()
        } finally {
            this.releaseReady()
        }
    }

    /** session_shutdown("quit"): release the ref; no attach after this. */
    quit(): void {
        this.state.quitting = true
        detach()
    }

    /** After a successful (re)connect. Attached: if the combiner is now a different
     *  process, attach() again (same function, same ref). Not attached (start()'s
     *  attach failed, or respawning stopped): attach once, to a running combiner. */
    async onReconnected(): Promise<void> {
        const att = this.state.attachment
        if (!att) {
            if (this.reconnectAttachTried) return
            this.reconnectAttachTried = true
            await this.attach({ onlyIfRunning: true })
            return
        }
        const status = await this.statusOf(att.binary, att.name)
        if (status.kind !== "running" || this.state.attachment !== att) return
        const ours = await this.attachedIdentity(att)
        if (!ours || sameCombiner(ours, status.identity)) return
        this.log("info", `combiner "${att.name}" is a new process (pid ${status.identity.pid}); attaching to it`)
        await this.attach({ onlyIfRunning: true })
    }

    /** Between refused-connect retries (the connection's liveness check):
     *  - ours, alive: "alive" (a blip), or "relaunched" while our own fresh launch
     *    is still booting, so the connect gets the cold-start budget;
     *  - a different process: attach to it (same function, same ref), "alive";
     *  - ours, stopped: respawn within the cap ("relaunched" / "gone");
     *  - unknown, or never attached: "alive". An unknown lookup is never dead, and
     *    with nothing of ours to judge there is nothing to respawn. */
    async onRefused(): Promise<Liveness> {
        if (this.state.quitting) return "gone"
        const att = this.state.attachment
        // No attachment: respawning stopped (fail fast; a combiner someone else starts
        // is picked up by onReconnected), or never attached (let the budget run).
        if (!att) return this.state.respawnStopped ? "gone" : "alive"
        const status = await this.statusOf(att.binary, att.name)
        if (this.state.quitting) return "gone"
        // Someone re-attached while we looked (another caller's respawn or attach).
        if (this.state.attachment !== att) return this.booting() ? "relaunched" : "alive"
        if (status.kind === "unknown") {
            // No answer from sharedserver is its bug: report it (once per streak) and
            // decide by our own pid check: gone means dead.
            if (status.noAnswer) this.reportNoAnswer(status.noAnswer)
            const known = att.identity
            if (!known || this.isAlive(known.pid)) return this.booting() ? "relaunched" : "alive"
            return this.respawn(att, known)
        }
        this.noAnswerReported = false
        const ours = await this.attachedIdentity(att)
        if (status.kind === "running") {
            if (!ours) return this.booting() ? "relaunched" : "alive"
            if (sameCombiner(ours, status.identity)) {
                // sharedserver can still read "active" just after a death: trust the pid.
                if (!this.isAlive(ours.pid)) return this.respawn(att, ours)
                return this.booting() ? "relaunched" : "alive"
            }
            // Someone else started a new combiner: same function, same ref.
            await this.attach({ onlyIfRunning: true })
            return "alive"
        }
        return this.respawn(att, ours)
    }

    /** Respawn our dead combiner with the one attach function, within the cap.
     *  Concurrent callers for the same death share one respawn. */
    private respawn(att: Attachment, dead: CombinerIdentity | undefined): Promise<Liveness> {
        const key = identityKey(dead)
        const inflight = this.state.respawning
        if (inflight && inflight.key === key) return inflight.promise
        const promise = (async (): Promise<Liveness> => {
            if (this.state.respawnStopped) return "gone"
            if (this.state.attachment !== att) return "alive" // someone already re-attached
            const t = Date.now()
            this.state.respawns = this.state.respawns.filter((at) => t - at < RESPAWN_WINDOW_MS)
            if (this.state.respawns.length >= RESPAWN_LIMIT) {
                return this.stopRespawning(
                    att,
                    `the combiner died ${RESPAWN_LIMIT + 1} times within ${RESPAWN_WINDOW_MS / 1000} s; not ` +
                        "restarting it again.",
                )
            }
            this.log("warn", "the combiner is not running; restarting it")
            const launchesBefore = this.state.launches
            const ok = await this.attach()
            // Only a restart that actually ran the combiner counts against the cap; a
            // restart that couldn't (sharedserver or the combiner can't be started)
            // counts separately, so it isn't reported as deaths.
            if (ok && this.state.launches !== launchesBefore) this.state.respawns.push(t)
            if (ok) {
                this.state.respawnFailures = 0
                return "relaunched"
            }
            this.state.respawnFailures++
            if (this.state.respawnFailures >= RESPAWN_LIMIT + 1) {
                return this.stopRespawning(
                    att,
                    `couldn't restart the combiner (${this.state.respawnFailures} attempts in a row); not trying again.`,
                )
            }
            return "gone"
        })().finally(() => {
            if (this.state.respawning?.promise === promise) this.state.respawning = undefined
        })
        this.state.respawning = { key, promise }
        return promise
    }

    /** Set after reporting that sharedserver gave no answer; cleared by an answer. */
    private noAnswerReported = false

    private reportNoAnswer(why: string): void {
        if (this.noAnswerReported) return
        this.noAnswerReported = true
        this.log(
            "warn",
            `sharedserver info gave no answer (${why}); judging the combiner by its process instead. ` +
                "sharedserver normally answers in a few ms, so this is worth reporting.",
        )
    }

    /** Stop respawning and report why, once. The dead process's ref died with it;
     *  dropping the attachment lets a combiner someone else starts later be picked up
     *  (onReconnected attaches to it). */
    private stopRespawning(att: Attachment, why: string): Liveness {
        this.state.respawnStopped = true
        if (this.state.attachment === att) this.state.attachment = null
        const logs = this.state.logs.length ? ` Its logs: ${this.state.logs.join(", ")}.` : ""
        this.log("error", `${why}${logs} Start a new pi session to try again.`)
        return "gone"
    }

    /** Record that this pi's attach launched the combiner (rather than attaching to
     *  a running one). Protected so a test subclass's scripted launch can call it. */
    protected noteLaunched(): void {
        this.state.launches++
        this.state.launchedAt = Date.now()
    }

    /** True while a combiner this pi launched is younger than COLD_START_MS. */
    private booting(): boolean {
        const at = this.state.launchedAt
        return at !== undefined && Date.now() - at < COLD_START_MS
    }

    /** The process our ref is on; fills it in if the attach-time lookup was unknown. */
    private async attachedIdentity(att: Attachment): Promise<CombinerIdentity | undefined> {
        if (att.identity) return att.identity
        const status = await this.statusOf(att.binary, att.name)
        if (status.kind === "running" && this.state.attachment === att && !att.identity) att.identity = status.identity
        return att.identity
    }

    /** Watch the combiner process this pi holds its ref on. Every WATCH_INTERVAL_MS,
     *  check its pid still exists (a syscall, no child process); every
     *  WATCH_STATUS_EVERY ticks also ask sharedserver, which catches a reused pid and
     *  a different process replacing ours. When ours is gone, call `onGone` once for
     *  that process: the caller reconnects, and the reconnect asks onRefused /
     *  onReconnected. A dropped connection isn't reliably reported when the combiner
     *  dies, so this is what makes recovery prompt. Returns a stop function; nothing
     *  is reported after it's called. */
    watch(onGone: () => void): () => void {
        let reportedFor: string | undefined
        let ticks = 0
        let stopped = false
        const report = (key: string) => {
            if (stopped || key === reportedFor) return
            reportedFor = key
            onGone()
        }
        const timer = setInterval(() => {
            ticks++
            void (async () => {
                const att = this.state.attachment
                if (!att || this.state.quitting) return
                const ours = att.identity
                if (!ours) {
                    // Identity not known yet: no pid to check. Ask sharedserver only on
                    // the periodic ticks, and report a stopped combiner even so.
                    if (ticks % WATCH_STATUS_EVERY !== 0) return
                    const status = await this.statusOf(att.binary, att.name)
                    if (this.state.attachment !== att) return
                    if (status.kind === "running" && !att.identity) att.identity = status.identity
                    else if (status.kind === "stopped") report("unknown")
                    return
                }
                const key = identityKey(ours)
                if (key === reportedFor) return
                if (!this.isAlive(ours.pid)) return report(key)
                if (ticks % WATCH_STATUS_EVERY !== 0) return
                const status = await this.statusOf(att.binary, att.name)
                if (this.state.attachment !== att) return
                if (status.kind === "stopped" || (status.kind === "running" && !sameCombiner(ours, status.identity)))
                    report(key)
            })()
        }, WATCH_INTERVAL_MS)
        timer.unref()
        return () => {
            stopped = true
            clearInterval(timer)
        }
    }

    /** Whether process `pid` exists (a seam for tests). EPERM means it exists but
     *  isn't ours to signal. */
    protected isAlive(pid: number): boolean {
        try {
            process.kill(pid, 0)
            return true
        } catch (e) {
            return (e as NodeJS.ErrnoException).code === "EPERM"
        }
    }

    /** What sharedserver says about the combiner now (a seam for tests). */
    protected statusOf(binary: string, name: string): Promise<CombinerStatus> {
        return combinerStatus(binary, name)
    }

    /** The ONE attach procedure: `sharedserver use` (launches the combiner when it
     *  isn't running, else just takes this pi process's ref), then records which
     *  process the ref is on. `onlyIfRunning`: attach to a running combiner, never
     *  launch one. Single-flight; resolves true when attached. */
    attach(opts: { onlyIfRunning?: boolean } = {}): Promise<boolean> {
        const launches = !opts.onlyIfRunning
        const inflight = this.state.attaching
        if (inflight) {
            // A launching caller never settles for an attach-only flight that failed.
            if (!launches || this.state.attachingLaunches) return inflight
            return inflight.then((ok) => (ok ? true : this.attach(opts)))
        }
        const flight = this.attachOnce(opts).finally(() => {
            if (this.state.attaching === flight) {
                this.state.attaching = undefined
                this.state.attachingLaunches = undefined
            }
        })
        this.state.attaching = flight
        this.state.attachingLaunches = launches
        return flight
    }

    /** The attach itself (resolve sharedserver + the combiner, `sharedserver use`,
     *  record the identity). Protected so tests can subclass it; nothing else
     *  overrides it. */
    protected async attachOnce(opts: { onlyIfRunning?: boolean }): Promise<boolean> {
        if (this.state.quitting) return false
        const log = this.log
        const binary = resolveSharedserver(
            {
                label: "mcp-combiner",
                minVersion: SHAREDSERVER_MIN_VERSION,
                installerUrl:
                    "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh",
            },
            env("SHAREDSERVER_BIN"),
            process.env,
            log,
        )
        if (!binary) {
            log("error", "sharedserver binary not found; set $SHAREDSERVER_BIN, or PI_MCP_COMBINER_MANAGE=false")
            return false
        }

        const combiner = resolveCombiner(log)
        if (!combiner) {
            log(
                "error",
                "mcp-combiner could not be found or fetched; install uv and it is fetched from PyPI on demand, " +
                    "or install mcp-combiner, or set $PI_MCP_COMBINER_COMMAND / $PI_MCP_COMBINER_CHECKOUT",
            )
            return false
        }

        const cfgPath = resolveConfig(log)
        if (!cfgPath) return false // resolveConfig already logged the specifics

        const name = env("PI_MCP_COMBINER_NAME") ?? DEFAULT_NAME
        // `use` launches a stopped server; the reconnect paths must only attach.
        if (opts.onlyIfRunning && (await this.statusOf(binary, name)).kind !== "running") return false
        if (this.state.quitting) return false
        const port = resolvePort(log)
        const grace = env("PI_MCP_COMBINER_GRACE") ?? DEFAULT_GRACE

        // Assemble: <combiner> [--mcp] --config <cfg> --port <port> [--host <host>]
        const serve: string[] = []
        const modern = combinerNeedsMcpFlag(combiner)
        if (modern) serve.push("--mcp")
        serve.push("--config", cfgPath, "--port", String(port))
        const host = env("PI_MCP_COMBINER_HOST")
        if (host) serve.push("--host", host)

        // Logging parity with the sibling plugins' two-file scheme: sharedserver captures
        // raw stdout/stderr (--log-file on `use`), and the combiner writes its own
        // --log-file. Both default under $XDG_STATE_HOME/mcp-combiner; "none" disables.
        // Combiner-side flags ride the same version gate as --mcp.
        const logDir = join(process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "mcp-combiner")
        const pyLogFile = env("PI_MCP_COMBINER_PYLOG") ?? join(logDir, "mcp-combiner-py.log")
        if (modern) {
            if (pyLogFile !== "none") serve.push("--log-file", pyLogFile)
            serve.push("--log-level", env("PI_MCP_COMBINER_LOG_LEVEL") ?? "info")
        }

        const wrappedArgs = [...combiner.args, ...(combiner.extra ?? []), ...serve]
        const useArgs = [
            "use",
            name,
            "--pid",
            String(process.pid),
            "--grace-period",
            grace,
            "--metadata",
            `pi-${process.pid}`,
        ]
        const logFile = env("PI_MCP_COMBINER_LOG") ?? join(logDir, "mcp-combiner.log")
        if (logFile !== "none") {
            try {
                mkdirSync(dirname(logFile), { recursive: true })
            } catch {
                // best-effort — a failed mkdir just means sharedserver may drop the capture
            }
            useArgs.push("--log-file", logFile)
        }
        useArgs.push("--", combiner.cmd, ...wrappedArgs)

        installProcessCleanup()
        // A combiner that just died reads "defunct" until sharedserver's watcher has
        // reaped it, and `use` refuses with "Retry shortly": retry a few times.
        for (let attempt = 1; ; attempt++) {
            const result = spawnSync(binary, useArgs, { stdio: "pipe", env: process.env, timeout: SHAREDSERVER_TIMEOUT_MS })
            if (result.error) {
                log("error", `${name}: failed to spawn sharedserver (${result.error.message})`)
                return false
            }
            if (result.status === 0) {
                // `use` says "Started server" when it launched the combiner itself.
                if (/Started server/.test(result.stdout?.toString() ?? "")) this.noteLaunched()
                break
            }
            const stderr = result.stderr?.toString().trim() ?? ""
            if (attempt < ATTACH_DEFUNCT_RETRIES && /defunct|retry shortly/i.test(stderr)) {
                await new Promise((resolve) => setTimeout(resolve, ATTACH_DEFUNCT_DELAY_MS).unref())
                // Quit may have released the combiner while we slept: don't re-take it.
                if (this.state.quitting) return false
                continue
            }
            log("error", `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`)
            return false
        }

        const attachment: Attachment = { binary, name, identity: undefined }
        this.state.attachment = attachment
        this.state.logs = [logFile, pyLogFile].filter((f) => f !== "none")
        void this.statusOf(binary, name).then((status) => {
            if (status.kind === "running" && this.state.attachment === attachment && !attachment.identity)
                attachment.identity = status.identity
        })
        log("info", `combiner "${name}" attached on port ${port} (${combiner.cmd} ${wrappedArgs.join(" ")})`)
        return true
    }
}
