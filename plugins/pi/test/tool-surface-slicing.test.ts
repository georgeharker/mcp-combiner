// Sliced native tool registration. pi rebuilds its whole tool registry (and, with
// codemode on, re-renders every codemode tool's declaration) on EVERY registerTool,
// so registering a ~1,000-tool catalog in one synchronous loop froze typing for
// ~10 s. activateNativeTools now registers in time-boxed slices (REGISTER_SLICE_MS)
// with a macrotask yield between them, and activateNativeMode serialises passes so a newer
// catalog supersedes an in-flight pass. These tests pin:
//
//   1. a pass yields to the event loop between slices (a macrotask queued before the
//      pass runs after exactly one slice), and a slice ends when its time budget is
//      spent;
//   2. a run() mid-pass stops the older pass at its next slice boundary, and the final
//      registry matches the newest catalog;
//   3. dispose() mid-pass: no registerTool after it;
//   4. a pass that stops early leaves `state` matching exactly what pi was given.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type {
    CommandSpec,
    ExecOptions,
    ExecResult,
    ExtensionAPI,
    SendMessage,
    SendMessageOptions,
    ToolDefinition,
} from "../src/pi.js"
import { NativeCombinerConnection, type ResourceSummary, type ToolSummary } from "../src/native/combiner-connection.js"
import { updateFooter } from "../src/client/footer.js"
import { activateNativeTools, REGISTER_SLICE_MS, type NativeToolSurfaceState } from "../src/native/tool-surface.js"
import { activateNativeMode, waitForRegistration, type NativeSurface } from "../src/native/index.js"

/** Fake main-thread clock: every registerTool "costs" COST_MS, like pi's registry
 *  rebuild, so a slice holds REGISTER_SLICE_MS / COST_MS tools. */
let clock = 0
const COST_MS = 10
const REGISTER_SLICE = REGISTER_SLICE_MS / COST_MS

/** Records every registerTool call; the last definition per name is pi's view. */
class RecordingPi implements ExtensionAPI {
    calls: ToolDefinition[] = []
    latest = new Map<string, ToolDefinition>()
    on(): void {}
    registerCommand(_name: string, _spec: CommandSpec): void {}
    registerTool(tool: ToolDefinition): void {
        clock += COST_MS
        this.calls.push(tool)
        this.latest.set(tool.name, tool)
    }
    sendUserMessage(_text: string): void {}
    async exec(_command: string, _args: string[], _options?: ExecOptions): Promise<ExecResult> {
        return { stdout: "", stderr: "", code: 0, killed: false }
    }
    sendMessage(_message: SendMessage, _options?: SendMessageOptions): void {}
    /** Names pi currently offers (registered and not retired as hidden). */
    live(): Set<string> {
        return new Set([...this.latest.values()].filter((d) => d.exposure !== "hidden").map((d) => d.name))
    }
}

/** A connection whose catalog the test swaps at will; never touches the network. */
class CatalogConnection extends NativeCombinerConnection {
    catalog: ToolSummary[] = []
    constructor() {
        super({ baseUrl: "http://127.0.0.1:9741/mcp" }, () => undefined)
    }
    override async listTools(): Promise<ToolSummary[]> {
        return this.catalog
    }
    /** Resource listing for the read_* pass; tests replace it to delay or fail it. */
    resourceList: () => Promise<ResourceSummary[]> = async () => []
    override listResources(): Promise<ResourceSummary[]> {
        return this.resourceList()
    }
}

const catalog = (prefix: string, n: number): ToolSummary[] =>
    Array.from({ length: n }, (_, i) => ({
        name: `${prefix}_tool${i}`,
        description: `tool ${i}`,
        inputSchema: { type: "object", properties: { x: { type: "string" } } },
    }))

const quiet = () => undefined
const nextMacrotask = () => new Promise<void>((resolve) => setImmediate(resolve))
/** Let every in-flight pass run to completion (each slice needs one macrotask). */
async function settle(): Promise<void> {
    for (let i = 0; i < 50; i++) await nextMacrotask()
}

beforeEach(() => {
    clock = 0
    vi.spyOn(performance, "now").mockImplementation(() => clock)
})
afterEach(() => {
    vi.restoreAllMocks()
})

describe("sliced native tool registration", () => {
    it("yields to the event loop between slices", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", REGISTER_SLICE * 2 + 10)
        let seenAtFirstTurn = -1
        const pass = activateNativeTools(pi, { connection: conn, log: quiet })
        // Queued before the pass's first yield: runs after exactly one slice. Without
        // the yield the whole catalog registers first and this sees all of it.
        setImmediate(() => {
            seenAtFirstTurn = pi.calls.length
        })
        await pass
        expect(seenAtFirstTurn).toBe(REGISTER_SLICE)
        expect(pi.calls.length).toBe(REGISTER_SLICE * 2 + 10)
    })

    it("a newer run() supersedes the in-flight pass; the newest catalog wins", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("old", REGISTER_SLICE * 4)
        const surface = activateNativeMode(pi, { connection: conn, exposeResources: false, log: quiet })
        surface.run()
        await nextMacrotask() // the first slice of the old catalog is in
        const fresh = [...catalog("old", 5), ...catalog("new", REGISTER_SLICE * 2)]
        conn.catalog = fresh
        surface.run()
        await settle()
        // The old pass stopped at its next slice boundary rather than finishing.
        const oldRegistrations = pi.calls.filter((d) => d.name.startsWith("old_") && d.exposure !== "hidden")
        expect(oldRegistrations.length).toBeLessThan(REGISTER_SLICE * 4)
        // pi's final view is exactly the newest catalog: nothing stale left live.
        expect(pi.live()).toEqual(new Set(fresh.map((t) => t.name)))
        surface.dispose()
    })

    it("dispose() mid-pass stops registration at the next slice", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", REGISTER_SLICE * 4)
        const surface = activateNativeMode(pi, { connection: conn, exposeResources: false, log: quiet })
        surface.run()
        await nextMacrotask()
        const atDispose = pi.calls.length
        surface.dispose()
        await settle()
        expect(atDispose).toBe(REGISTER_SLICE)
        expect(pi.calls.length).toBe(atDispose)
    })

    it("a pass that stops early leaves state matching what pi holds", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", REGISTER_SLICE * 3)
        const state: NativeToolSurfaceState = { names: new Set(), signatures: new Map() }
        let go = true
        const pass = activateNativeTools(pi, { connection: conn, state, shouldContinue: () => go, log: quiet })
        setImmediate(() => {
            go = false
        })
        await pass
        expect(pi.calls.length).toBe(REGISTER_SLICE)
        expect(state.names).toEqual(pi.live())
        expect([...state.signatures.keys()].sort()).toEqual([...state.names].sort())

        // The next pass resumes: it registers only what the stopped one missed.
        const before = pi.calls.length
        await activateNativeTools(pi, { connection: conn, state, log: quiet })
        expect(pi.calls.length - before).toBe(REGISTER_SLICE * 2)
        expect(pi.live()).toEqual(new Set(conn.catalog.map((t) => t.name)))
    })
})

describe("registration progress", () => {
    it("the newest pass always ends with a finished report", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", REGISTER_SLICE * 3)
        const reports: Array<[number, number]> = []
        const surface = activateNativeMode(pi, {
            connection: conn,
            exposeResources: false,
            onToolProgress: (done, total) => reports.push([done, total]),
            log: quiet,
        })
        surface.run()
        await nextMacrotask()
        // Superseded mid-pass by a pass with NOTHING to register (same catalog, the
        // first slice is already in, so it registers the remaining two slices) ...
        surface.run()
        await settle()
        const last = reports.at(-1)
        expect(last && last[0] >= last[1]).toBe(true)
        // ... and once more with a fully registered catalog: no slices at all, but
        // the pass still reports finished.
        reports.length = 0
        surface.run()
        await settle()
        expect(reports).toEqual([[0, 0]])
        surface.dispose()
    })
})

describe("time-boxed slices", () => {
    it("a slice stops at its time budget, whatever the per-call cost", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", 40)
        // Calls get slower as the pass goes (pi's rebuild grows with the registry):
        // 5 ms each for the first 20, then 25 ms each.
        const register = pi.registerTool.bind(pi)
        pi.registerTool = (tool) => {
            register(tool)
            clock += pi.calls.length <= 20 ? 5 - COST_MS : 25 - COST_MS
        }
        const sliceSizes: number[] = []
        let prev = 0
        await activateNativeTools(pi, {
            connection: conn,
            onProgress: (done) => {
                sliceSizes.push(done - prev)
                prev = done
            },
            log: quiet,
        })
        // Cheap calls: 10 per 50 ms slice. Expensive calls: 2 per slice. A fixed
        // count per slice would give the same size throughout.
        expect(sliceSizes.slice(0, 2)).toEqual([10, 10])
        expect(sliceSizes.slice(2).every((n) => n === 2)).toBe(true)
        expect(sliceSizes.reduce((a, b) => a + b, 0)).toBe(40)
    })

    it("a call slower than the whole budget still registers (one per slice)", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", 3)
        const register = pi.registerTool.bind(pi)
        pi.registerTool = (tool) => {
            register(tool)
            clock += REGISTER_SLICE_MS * 2
        }
        const sizes: number[] = []
        let prev = 0
        await activateNativeTools(pi, {
            connection: conn,
            onProgress: (done) => {
                sizes.push(done - prev)
                prev = done
            },
            log: quiet,
        })
        expect(sizes).toEqual([1, 1, 1])
    })
})

describe("turn wait: busy() / settled() / waitForRegistration", () => {
    it("busy while a pass runs; settled when it ends", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", REGISTER_SLICE * 3)
        const surface = activateNativeMode(pi, { connection: conn, exposeResources: false, log: quiet })
        expect(surface.busy()).toBe(false)
        surface.run()
        expect(surface.busy()).toBe(true)
        await surface.settled()
        expect(surface.busy()).toBe(false)
        expect(pi.live().size).toBe(REGISTER_SLICE * 3)
        surface.dispose()
    })

    it("settled() follows a superseding pass to the newest catalog", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("old", REGISTER_SLICE * 4)
        const surface = activateNativeMode(pi, { connection: conn, exposeResources: false, log: quiet })
        surface.run()
        const waiting = surface.settled()
        await nextMacrotask()
        const fresh = catalog("new", REGISTER_SLICE * 2)
        conn.catalog = fresh
        surface.run()
        await waiting
        // Resolved only once the NEWEST pass finished: every new tool is in.
        expect(pi.live()).toEqual(new Set(fresh.map((t) => t.name)))
        expect(surface.busy()).toBe(false)
        surface.dispose()
    })

    it("settled() resolves when the pass fails", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.listTools = () => Promise.reject(new Error("cannot reach the combiner"))
        const surface = activateNativeMode(pi, { connection: conn, exposeResources: false, log: quiet })
        surface.run()
        await surface.settled()
        expect(surface.busy()).toBe(false)
        surface.dispose()
    })

    it("waitForRegistration: idle at once, settled, timeout, aborted", async () => {
        const idle: Pick<NativeSurface, "busy" | "settled"> = { busy: () => false, settled: async () => undefined }
        expect(await waitForRegistration(idle)).toBe("idle")

        let finish: () => void = () => undefined
        const pending = new Promise<void>((r) => {
            finish = r
        })
        const running: Pick<NativeSurface, "busy" | "settled"> = { busy: () => true, settled: () => pending }

        vi.restoreAllMocks() // real timers/clock for the timeout case
        expect(await waitForRegistration(running, { timeoutMs: 20 })).toBe("timeout")

        const ac = new AbortController()
        const aborted = waitForRegistration(running, { timeoutMs: 5_000, signal: ac.signal })
        ac.abort()
        expect(await aborted).toBe("aborted")

        const settled = waitForRegistration(running, { timeoutMs: 5_000 })
        finish()
        expect(await settled).toBe("settled")
    })
})

describe("review fixes", () => {
    it("removals are time-boxed too", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        const state: NativeToolSurfaceState = { names: new Set(), signatures: new Map() }
        conn.catalog = catalog("srv", REGISTER_SLICE * 4)
        await activateNativeTools(pi, { connection: conn, state, log: quiet })
        conn.catalog = [] // the whole server went away
        const before = pi.calls.length
        let seenAtFirstTurn = -1
        const pass = activateNativeTools(pi, { connection: conn, state, log: quiet })
        setImmediate(() => {
            seenAtFirstTurn = pi.calls.length - before
        })
        await pass
        expect(seenAtFirstTurn).toBe(REGISTER_SLICE)
        expect(pi.live().size).toBe(0)
        expect(state.names.size).toBe(0)
    })

    it("a pass that fails after dispose() is not reported", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        let fail: (e: Error) => void = () => undefined
        conn.listTools = () =>
            new Promise((_, reject) => {
                fail = reject
            })
        const warnings: string[] = []
        const surface = activateNativeMode(pi, {
            connection: conn,
            exposeResources: false,
            log: (level, message) => {
                if (level === "warn") warnings.push(message)
            },
        })
        surface.run()
        surface.dispose() // session_shutdown
        fail(new Error("This extension ctx is stale"))
        await surface.settled()
        expect(warnings).toEqual([])
    })

    it("settled() waits for the read_* resource pass too", async () => {
        const pi = new RecordingPi()
        const conn = new CatalogConnection()
        conn.catalog = catalog("srv", 3)
        let release: () => void = () => undefined
        conn.resourceList = () =>
            new Promise((resolve) => {
                release = () => resolve([])
            })
        const surface = activateNativeMode(pi, { connection: conn, log: quiet })
        surface.run()
        let settled = false
        void surface.settled().then(() => {
            settled = true
        })
        await settle() // the tool pass is long done
        expect(settled).toBe(false)
        expect(surface.busy()).toBe(true)
        release()
        await settle()
        expect(settled).toBe(true)
        surface.dispose()
    })

    it("waitForRegistration: onInterrupt ends the wait and is unsubscribed", async () => {
        vi.restoreAllMocks()
        const running: Pick<NativeSurface, "busy" | "settled"> = {
            busy: () => true,
            settled: () => new Promise<void>(() => undefined),
        }
        let fire: () => void = () => undefined
        let unsubscribed = false
        const waiting = waitForRegistration(running, {
            timeoutMs: 5_000,
            onInterrupt: (interrupt) => {
                fire = interrupt
                return () => {
                    unsubscribed = true
                }
            },
        })
        fire() // Esc
        expect(await waiting).toBe("aborted")
        expect(unsubscribed).toBe(true)
    })

    it("footer: a slow paint that resolves last shows the progress as it is then", async () => {
        class SlowHealth extends CatalogConnection {
            pending: Array<() => void> = []
            override health(): Promise<unknown> {
                return new Promise((resolve) => this.pending.push(() => resolve({ servers: { a: { state: "ready" } } })))
            }
        }
        const conn = new SlowHealth()
        conn.catalog = catalog("srv", 7)
        let shown: string | undefined
        const ui = { setStatus: (_key: string, text: string | undefined) => (shown = text) }
        let registering: { done: number; total: number } | undefined = { done: 3, total: 7 }
        const early = updateFooter(conn, ui, "mcp", "full", () => registering) // starts mid-pass
        registering = undefined // the pass finished
        const late = updateFooter(conn, ui, "mcp", "full", () => registering)
        await new Promise((r) => setTimeout(r, 0))
        conn.pending[1]() // the later paint resolves first...
        await late
        conn.pending[0]() // ...the earlier one resolves last
        await early
        expect(shown).toContain("7 tools")
        expect(shown).not.toContain("registering")
    })
})
