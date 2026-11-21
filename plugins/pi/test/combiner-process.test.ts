// CombinerProcess: the one place pi launches, attaches to, checks and respawns the
// combiner (design: pi-one-combiner-process-module-one-attach-function-for-launch-
// ref-and-respawn-respawn-capped-at-2-per-minute.md). The attach and the identity
// lookup are the seams: a real subclass replaces them, so no sharedserver runs.
//
//   - onRefused: ours alive -> "alive"; a different process -> attach (same
//     function, same ref) -> "alive"; dead -> respawn -> "relaunched", at most
//     RESPAWN_LIMIT per RESPAWN_WINDOW_MS, then "gone" with one report;
//     start() lifts the stop.
//   - onReconnected: same process -> nothing; different -> attach; never attached
//     -> attach once only.
//   - attach is single-flight; quitting stops everything.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
    COMBINER_PROCESS_STATE,
    CombinerProcess,
    RESPAWN_LIMIT,
    RESPAWN_WINDOW_MS,
    WATCH_INTERVAL_MS,
    WATCH_STATUS_EVERY,
    parseCombinerStatus,
    type CombinerIdentity,
    type CombinerStatus,
    type LaunchPlan,
    type LogFn,
} from "../src/combiner-process.js"

type Logged = { level: string; message: string }

/** A CombinerProcess whose sharedserver is a script: `running` is the combiner
 *  process sharedserver reports now (undefined = not running), `lookupFails` makes
 *  every lookup come back unknown, and an attach "launches" `nextLaunch` when
 *  nothing runs (launch fails if it is undefined). `lookupGate`, when set, holds
 *  every lookup until released (to line up concurrent callers). */
class ScriptedProcess extends CombinerProcess {
    running: CombinerIdentity | undefined
    nextLaunch: CombinerIdentity | undefined = { pid: 200, startTime: 2 }
    attachCalls: Array<{ onlyIfRunning?: boolean }> = []
    /** Pids watch() should see as gone. */
    deadPids = new Set<number>()
    lookupFails = false
    lookupGate: Promise<void> | undefined
    logged: Logged[]

    constructor(running: CombinerIdentity | undefined, logged: Logged[] = []) {
        const log: LogFn = (level, message) => logged.push({ level, message })
        super(log)
        this.running = running
        this.logged = logged
    }

    protected override isAlive(pid: number): boolean {
        return !this.deadPids.has(pid)
    }

    protected override async statusOf(): Promise<CombinerStatus> {
        if (this.lookupGate) await this.lookupGate
        if (this.lookupFails) return { kind: "unknown", noAnswer: "no answer within 500 ms" }
        return this.running ? { kind: "running", identity: this.running } : { kind: "stopped" }
    }

    protected override async attachOnce(opts: { onlyIfRunning?: boolean }): Promise<boolean> {
        this.attachCalls.push(opts)
        if (this.state.quitting) return false
        if (!this.running) {
            if (opts.onlyIfRunning || !this.nextLaunch) return false
            this.running = this.nextLaunch // `use` launched it
            this.noteLaunched()
        }
        this.state.attachment = { binary: "sharedserver", name: "mcp-combiner", identity: this.running }
        return true
    }
}

const ours: CombinerIdentity = { pid: 100, startTime: 1 }
const someoneElses: CombinerIdentity = { pid: 300, startTime: 3 }
const quiet: LogFn = () => undefined

beforeEach(() => {
    delete (globalThis as Record<symbol, unknown>)[COMBINER_PROCESS_STATE]
})
afterEach(() => {
    vi.restoreAllMocks()
    delete (globalThis as Record<symbol, unknown>)[COMBINER_PROCESS_STATE]
})

/** A process attached to `ours`, via start(). */
async function attachedTo(identity: CombinerIdentity): Promise<ScriptedProcess> {
    const proc = new ScriptedProcess(identity)
    await proc.start(quiet)
    proc.attachCalls = []
    return proc
}

describe("start and ready", () => {
    it("start() attaches (launching when nothing runs) and releases ready", async () => {
        const proc = new ScriptedProcess(undefined)
        let ready = false
        void proc.ready.then(() => {
            ready = true
        })
        await proc.start(quiet)
        expect(proc.attached).toBe(true)
        expect(proc.attachCalls).toEqual([{}])
        await Promise.resolve()
        expect(ready).toBe(true)
    })

    it("ready is released even when the attach fails", async () => {
        const proc = new ScriptedProcess(undefined)
        proc.nextLaunch = undefined
        await proc.start(quiet)
        expect(proc.attached).toBe(false)
        await expect(proc.ready).resolves.toBeUndefined()
    })

    it("a later session in the same pi process doesn't attach again", async () => {
        await attachedTo(ours)
        const next = new ScriptedProcess(ours) // a fresh extension instance, same process
        await next.start(quiet)
        expect(next.attachCalls).toEqual([])
        expect(next.attached).toBe(true)
    })
})

describe("onRefused", () => {
    it("our process still alive: keep retrying, no attach", async () => {
        const proc = await attachedTo(ours)
        expect(await proc.onRefused()).toBe("alive")
        expect(proc.attachCalls).toEqual([])
    })

    it("a different process: attach to it (same function, same ref), keep retrying", async () => {
        const proc = await attachedTo(ours)
        proc.running = someoneElses
        expect(await proc.onRefused()).toBe("alive")
        expect(proc.attachCalls).toEqual([{ onlyIfRunning: true }])
    })

    it("dead: respawn with the same attach function", async () => {
        const proc = await attachedTo(ours)
        proc.running = undefined
        expect(await proc.onRefused()).toBe("relaunched")
        expect(proc.attachCalls).toEqual([{}])
        expect(proc.running).toEqual(proc.nextLaunch)
    })

    it(`dead again and again: ${RESPAWN_LIMIT} respawns per window, then stop and report once`, async () => {
        let now = 1_000_000
        vi.spyOn(Date, "now").mockImplementation(() => now)
        const logged: Logged[] = []
        const proc = new ScriptedProcess(ours, logged)
        await proc.start((level, message) => logged.push({ level, message }))
        proc.attachCalls = []
        for (let i = 0; i < RESPAWN_LIMIT; i++) {
            proc.running = undefined // it died
            expect(await proc.onRefused()).toBe("relaunched")
            now += 5_000
        }
        proc.running = undefined // dies a third time within the minute
        expect(await proc.onRefused()).toBe("gone")
        expect(await proc.onRefused()).toBe("gone") // stays stopped
        expect(proc.attachCalls.length).toBe(RESPAWN_LIMIT)
        const errors = logged.filter((l) => l.level === "error")
        expect(errors.length).toBe(1)
        expect(errors[0].message).toMatch(/died 3 times/)
        expect(errors[0].message).not.toMatch(/mcp-combiner restart/)
    })

    it("respawns older than the window don't count", async () => {
        let now = 1_000_000
        vi.spyOn(Date, "now").mockImplementation(() => now)
        const proc = await attachedTo(ours)
        for (let i = 0; i < RESPAWN_LIMIT; i++) {
            proc.running = undefined
            expect(await proc.onRefused()).toBe("relaunched")
        }
        now += RESPAWN_WINDOW_MS + 1
        proc.running = undefined
        expect(await proc.onRefused()).toBe("relaunched")
    })

    it("an explicit start() lifts a respawn stop", async () => {
        const proc = await attachedTo(ours)
        for (let i = 0; i <= RESPAWN_LIMIT; i++) {
            proc.running = undefined
            await proc.onRefused()
        }
        expect(await proc.onRefused()).toBe("gone")
        await proc.start(quiet) // a new session
        proc.running = undefined
        expect(await proc.onRefused()).toBe("relaunched")
    })

    it("a respawn that fails reports gone", async () => {
        const proc = await attachedTo(ours)
        proc.running = undefined
        proc.nextLaunch = undefined
        expect(await proc.onRefused()).toBe("gone")
    })

    it("after quit: gone, and no attach", async () => {
        const proc = await attachedTo(ours)
        proc.quit()
        proc.running = undefined
        expect(await proc.onRefused()).toBe("gone")
        expect(proc.attachCalls).toEqual([])
    })
})

describe("onReconnected", () => {
    it("same process: nothing", async () => {
        const proc = await attachedTo(ours)
        await proc.onReconnected()
        expect(proc.attachCalls).toEqual([])
    })

    it("a different process: attach to it, running only", async () => {
        const proc = await attachedTo(ours)
        proc.running = someoneElses
        await proc.onReconnected()
        expect(proc.attachCalls).toEqual([{ onlyIfRunning: true }])
        await proc.onReconnected() // now attached to it: nothing more
        expect(proc.attachCalls.length).toBe(1)
    })

    it("never attached (start's attach failed): attach once, not on every reconnect", async () => {
        const proc = new ScriptedProcess(undefined)
        proc.nextLaunch = undefined
        await proc.start(quiet)
        proc.attachCalls = []
        // Reconnected, but the attach fails again (sharedserver doesn't run it under
        // our name): the next reconnect must not retry the attach.
        await proc.onReconnected()
        await proc.onReconnected()
        expect(proc.attachCalls).toEqual([{ onlyIfRunning: true }])
        expect(proc.attached).toBe(false)
    })
})

describe("attach", () => {
    it("is single-flight: concurrent callers share one attach", async () => {
        const proc = new ScriptedProcess(ours)
        const [a, b] = await Promise.all([proc.attach(), proc.attach()])
        expect(a && b).toBe(true)
        expect(proc.attachCalls.length).toBe(1)
    })
})

describe("watch", () => {
    it("reports once when the combiner process this pi holds a ref on exits", async () => {
        vi.useFakeTimers()
        try {
            const proc = await attachedTo(ours)
            let gone = 0
            const stop = proc.watch(() => gone++)
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS * 2)
            expect(gone).toBe(0) // alive
            proc.deadPids.add(ours.pid)
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS)
            expect(gone).toBe(1)
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS * 3)
            expect(gone).toBe(1) // once per process
            stop()
        } finally {
            vi.useRealTimers()
        }
    })

    it("stop() ends the watch", async () => {
        vi.useFakeTimers()
        try {
            const proc = await attachedTo(ours)
            let gone = 0
            proc.watch(() => gone++)()
            proc.deadPids.add(ours.pid)
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS * 3)
            expect(gone).toBe(0)
        } finally {
            vi.useRealTimers()
        }
    })
})

describe("review fixes", () => {
    it("no answer from sharedserver, our pid alive: alive, no respawn, reported once", async () => {
        const logged: Logged[] = []
        const proc = new ScriptedProcess(ours, logged)
        await proc.start((level, message) => logged.push({ level, message }))
        proc.attachCalls = []
        proc.lookupFails = true
        expect(await proc.onRefused()).toBe("alive")
        expect(await proc.onRefused()).toBe("alive")
        expect(proc.attachCalls).toEqual([])
        expect(logged.filter((l) => /gave no answer/.test(l.message)).length).toBe(1)
    })

    it("no answer from sharedserver, our pid gone: dead, respawn", async () => {
        const proc = await attachedTo(ours)
        proc.lookupFails = true
        proc.deadPids.add(ours.pid)
        expect(await proc.onRefused()).toBe("relaunched")
        expect(proc.attachCalls).toEqual([{}])
    })

    it("never attached: nothing to respawn", async () => {
        const proc = new ScriptedProcess(undefined)
        proc.nextLaunch = undefined
        await proc.start(quiet) // the attach fails
        proc.attachCalls = []
        expect(await proc.onRefused()).toBe("alive")
        expect(proc.attachCalls).toEqual([])
    })

    it("one death seen by several callers at once counts as one respawn", async () => {
        const logged: Logged[] = []
        const proc = new ScriptedProcess(ours, logged)
        await proc.start((level, message) => logged.push({ level, message }))
        proc.attachCalls = []
        proc.running = undefined
        let release: () => void = () => undefined
        proc.lookupGate = new Promise((r) => {
            release = r
        })
        const calls = [proc.onRefused(), proc.onRefused(), proc.onRefused()]
        await Promise.resolve()
        proc.lookupGate = undefined
        release()
        const results = await Promise.all(calls)
        expect(results.every((r) => r === "relaunched" || r === "alive")).toBe(true)
        expect(proc.attachCalls.length).toBe(1)
        expect(logged.filter((l) => l.level === "error")).toEqual([])
        // ... so the cap still allows another respawn for a second death.
        proc.running = undefined
        expect(await proc.onRefused()).toBe("relaunched")
    })

    it("a launching attach doesn't settle for a failed attach-only one in flight", async () => {
        const proc = new ScriptedProcess(undefined)
        let release: () => void = () => undefined
        proc.lookupGate = new Promise((r) => {
            release = r
        })
        // onlyIfRunning in flight, nothing running...
        const attachOnly = proc.attach({ onlyIfRunning: true })
        const launching = proc.attach()
        proc.lookupGate = undefined
        release()
        expect(await attachOnly).toBe(false)
        expect(await launching).toBe(true)
        expect(proc.attached).toBe(true)
    })

    it("after respawning stops, a combiner someone else starts is picked up", async () => {
        const proc = await attachedTo(ours)
        for (let i = 0; i <= RESPAWN_LIMIT; i++) {
            proc.running = undefined
            await proc.onRefused()
        }
        expect(proc.attached).toBe(false) // the dead attachment was dropped
        proc.running = someoneElses
        await proc.onReconnected()
        expect(proc.attached).toBe(true)
    })

    it("our own fresh launch still booting: refused connects get the cold-start budget", async () => {
        const proc = new ScriptedProcess(undefined)
        await proc.start(quiet)
        // ScriptedProcess doesn't run `use`; mark the launch as ours, now.
        ;(proc as unknown as { state: { launchedAt?: number } }).state.launchedAt = Date.now()
        expect(await proc.onRefused()).toBe("relaunched")
        ;(proc as unknown as { state: { launchedAt?: number } }).state.launchedAt = Date.now() - 60_000
        expect(await proc.onRefused()).toBe("alive")
    })

    it("watch: a reused pid or a replacement is caught by the periodic status check", async () => {
        vi.useFakeTimers()
        try {
            const proc = await attachedTo(ours)
            let gone = 0
            const stop = proc.watch(() => gone++)
            proc.running = someoneElses // our pid still "alive" (reused), but sharedserver runs another
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS * (WATCH_STATUS_EVERY - 1))
            expect(gone).toBe(0)
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS)
            expect(gone).toBe(1)
            stop()
        } finally {
            vi.useRealTimers()
        }
    })

    it("watch: nothing is reported after stop(), even from a tick already running", async () => {
        vi.useFakeTimers()
        try {
            const proc = await attachedTo(ours)
            let gone = 0
            let release: () => void = () => undefined
            proc.lookupGate = new Promise((r) => {
                release = r
            })
            const stop = proc.watch(() => gone++)
            proc.running = undefined
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS * WATCH_STATUS_EVERY) // a status tick is waiting
            stop()
            proc.lookupGate = undefined
            release()
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS)
            expect(gone).toBe(0)
        } finally {
            vi.useRealTimers()
        }
    })
})

describe("parseCombinerStatus", () => {
    it("reads sharedserver info --json", () => {
        expect(parseCombinerStatus('{"name":"x","state":"stopped"}')).toEqual({ kind: "stopped" })
        expect(parseCombinerStatus('{"state":"defunct","pid":5,"watcher_pid":4}')).toEqual({ kind: "stopped" })
        expect(parseCombinerStatus('{"state":"active","pid":5,"watcher_pid":4,"start_time":9}')).toEqual({
            kind: "running",
            identity: { pid: 5, startTime: 9 },
        })
        expect(parseCombinerStatus('{"state":"grace","pid":5,"watcher_pid":4}')).toEqual({
            kind: "running",
            identity: { pid: 5, startTime: undefined },
        })
        // a start still publishing (placeholder pid), garbage, odd states: unknown
        expect(parseCombinerStatus('{"state":"active","pid":5,"watcher_pid":null}')).toEqual({ kind: "unknown" })
        expect(parseCombinerStatus("not json")).toEqual({ kind: "unknown", noAnswer: "unparseable output" })
        expect(parseCombinerStatus('{"state":"weird"}')).toEqual({ kind: "unknown" })
    })
})

describe("final review fixes", () => {
    it("failed restarts don't count as deaths; three in a row stop with their own message", async () => {
        const logged: Logged[] = []
        const proc = new ScriptedProcess(ours, logged)
        await proc.start((level, message) => logged.push({ level, message }))
        proc.running = undefined
        proc.nextLaunch = undefined // the combiner can't be started
        expect(await proc.onRefused()).toBe("gone")
        expect(await proc.onRefused()).toBe("gone")
        expect(await proc.onRefused()).toBe("gone")
        const errors = logged.filter((l) => l.level === "error")
        expect(errors.length).toBe(1)
        expect(errors[0].message).toMatch(/couldn't restart the combiner/)
        expect(errors[0].message).not.toMatch(/died/)
    })

    it("sharedserver says running (same process) but our pid is gone: dead, respawn", async () => {
        const proc = await attachedTo(ours)
        proc.deadPids.add(ours.pid) // sharedserver still reads "active" for a moment
        expect(await proc.onRefused()).toBe("relaunched")
        expect(proc.attachCalls).toEqual([{}])
    })

    it("watch with an unknown identity: no lookups between status ticks; a stopped combiner is reported", async () => {
        vi.useFakeTimers()
        try {
            const proc = new ScriptedProcess(ours)
            await proc.start(quiet)
            ;(proc as unknown as { state: { attachment: { identity?: unknown } } }).state.attachment.identity = undefined
            let lookups = 0
            const statusOf = (proc as unknown as { statusOf: () => Promise<CombinerStatus> }).statusOf.bind(proc)
            ;(proc as unknown as { statusOf: () => Promise<CombinerStatus> }).statusOf = () => {
                lookups++
                return statusOf()
            }
            let gone = 0
            const stop = proc.watch(() => gone++)
            proc.running = undefined
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS * (WATCH_STATUS_EVERY - 1))
            expect(lookups).toBe(0)
            await vi.advanceTimersByTimeAsync(WATCH_INTERVAL_MS)
            expect(lookups).toBe(1)
            expect(gone).toBe(1)
            stop()
        } finally {
            vi.useRealTimers()
        }
    })
})

/** The REAL attachOnce (plan -> use -> record), with sharedserver's replies scripted:
 *  each `use` call takes the next reply. */
class ScriptedUse extends CombinerProcess {
    replies: Array<{ status: number | null; stdout?: string; stderr?: string; error?: Error }> = []
    useCalls: string[][] = []
    planned: LaunchPlan | undefined = {
        binary: "sharedserver",
        name: "mcp-combiner",
        port: 9741,
        useArgs: ["use", "mcp-combiner", "--pid", "1"],
        logs: ["/tmp/a.log"],
        describe: "mcp-combiner --mcp",
    }
    logged: Logged[] = []
    constructor() {
        const logged: Logged[] = []
        super((level, message) => logged.push({ level, message }))
        this.logged = logged
    }
    protected override plan(): LaunchPlan | undefined {
        return this.planned
    }
    protected override runSharedserver(_binary: string, args: string[]) {
        this.useCalls.push(args)
        return this.replies.shift() ?? { status: 1, stderr: "no scripted reply" }
    }
    protected override statusOf(): Promise<CombinerStatus> {
        return Promise.resolve({ kind: "running", identity: ours })
    }
    get launchedAt(): number | undefined {
        return (this as unknown as { state: { launchedAt?: number } }).state.launchedAt
    }
}

describe("attachOnce (the real one)", () => {
    it("a use that launched ('Started server') is noted as our launch", async () => {
        const proc = new ScriptedUse()
        proc.replies = [{ status: 0, stdout: "Started server mcp-combiner (PID: 7, refcount: 1)" }]
        expect(await proc.attach()).toBe(true)
        expect(proc.attached).toBe(true)
        expect(proc.launchedAt).toBeDefined()
    })

    it("a use that only attached isn't a launch", async () => {
        const proc = new ScriptedUse()
        proc.replies = [{ status: 0, stdout: "Attached to mcp-combiner (refcount: 3)" }]
        expect(await proc.attach()).toBe(true)
        expect(proc.launchedAt).toBeUndefined()
    })

    it("retries while sharedserver says defunct, then attaches", async () => {
        const proc = new ScriptedUse()
        proc.replies = [
            { status: 1, stderr: "shutting down (defunct, cleanup pending). Retry shortly" },
            { status: 0, stdout: "Started server mcp-combiner" },
        ]
        expect(await proc.attach()).toBe(true)
        expect(proc.useCalls.length).toBe(2)
    })

    it("a failing use is logged and leaves no attachment", async () => {
        const proc = new ScriptedUse()
        proc.replies = [{ status: 2, stderr: "boom" }]
        expect(await proc.attach()).toBe(false)
        expect(proc.attached).toBe(false)
        expect(proc.logged.some((l) => l.level === "error" && /use exited 2 \(boom\)/.test(l.message))).toBe(true)
    })

    it("quit during a defunct retry: no attach after it", async () => {
        const proc = new ScriptedUse()
        proc.replies = [
            { status: 1, stderr: "defunct. Retry shortly" },
            { status: 0, stdout: "Started server mcp-combiner" },
        ]
        const pending = proc.attach()
        proc.quit()
        expect(await pending).toBe(false)
        expect(proc.attached).toBe(false)
        expect(proc.useCalls.length).toBe(1)
    })

    it("nothing to launch (no plan): no use at all", async () => {
        const proc = new ScriptedUse()
        proc.planned = undefined
        expect(await proc.attach()).toBe(false)
        expect(proc.useCalls).toEqual([])
    })
})
