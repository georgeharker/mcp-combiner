// Connect readiness. `sharedserver use` returns once the combiner process is
// LAUNCHED, not once it listens, so the first connect after an attach can be
// refused. NativeCombinerConnection therefore:
//
//   1. waits for the start gate (the manage half's attach) before any connect;
//   2. retries a REFUSED connect (ECONNREFUSED) with backoff, within a budget;
//   3. does not retry other errors;
//   4. stops early when the liveness check says the combiner is gone, or when a
//      reset supersedes the connect.
import { describe, expect, it } from "vitest"
import type { JsonRpcMessage, McpTransport } from "@earendil-works/pi-mcp"
import { createInMemoryTransportPair, type InMemoryTransport } from "@earendil-works/pi-mcp/testing"
import {
    isConnectionRefused,
    NativeCombinerConnection,
    type AutoReconnect,
    type ConnectRetry,
    type ResolvedConnection,
} from "../src/native/combiner-connection.js"
import type { LogFn } from "../src/client/types.js"

type RpcEnvelope = { id?: unknown; method?: string; params?: unknown }

/** What Node's fetch raises when nothing listens on the port. */
function refusedError(): Error {
    return Object.assign(new TypeError("fetch failed"), {
        cause: Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:9741"), { code: "ECONNREFUSED" }),
    })
}

/** A fake combiner that answers initialize. `refuse` connects are refused at the
 *  socket first (start() rejects, as StreamableHttpTransport's fetch does); `fail`
 *  makes every connect fail with a non-refused error instead. */
class Combiner {
    attempts = 0
    /** The server legs of live connections, so a test can drop them. */
    private legs: InMemoryTransport[] = []
    constructor(
        public refuse = 0,
        public fail = false,
    ) {}

    transport(): McpTransport {
        this.attempts++
        if (this.fail) return new FailingTransport(new Error("HTTP 500 from combiner"))
        if (this.refuse > 0) {
            this.refuse--
            return new FailingTransport(refusedError())
        }
        const pair = createInMemoryTransportPair()
        pair.server.onMessage((raw) => this.handle(pair.server, raw as RpcEnvelope))
        void pair.server.start()
        this.legs.push(pair.server)
        return pair.client
    }

    /** The combiner goes away: every live connection drops. */
    async dropAll(): Promise<void> {
        const legs = this.legs
        this.legs = []
        await Promise.all(legs.map((leg) => leg.close()))
    }

    private handle(server: InMemoryTransport, msg: RpcEnvelope): void {
        if (typeof msg.id !== "number" || msg.method !== "initialize") return
        void server.send({
            jsonrpc: "2.0",
            id: msg.id,
            result: {
                protocolVersion: (msg.params as { protocolVersion?: string } | undefined)?.protocolVersion,
                capabilities: {},
                serverInfo: { name: "fake-combiner", version: "0" },
            },
        } as unknown as JsonRpcMessage)
    }
}

/** A transport whose start() fails with `error` (a refused or failed connect). */
class FailingTransport implements McpTransport {
    constructor(private readonly error: Error) {}
    start(): Promise<void> {
        return Promise.reject(this.error)
    }
    send(): Promise<void> {
        return Promise.reject(this.error)
    }
    close(): Promise<void> {
        return Promise.resolve()
    }
    onMessage(): () => void {
        return () => undefined
    }
    onError(): () => void {
        return () => undefined
    }
    onClose(): () => void {
        return () => undefined
    }
}

const inputs: ResolvedConnection = { baseUrl: "http://127.0.0.1:9741/mcp" }
const quiet: LogFn = () => undefined
const fastRetry: ConnectRetry = { budgetMs: 2_000, reconnectBudgetMs: 2_000, firstMs: 10, maxMs: 20 }

function connection(combiner: Combiner, retry: ConnectRetry = fastRetry): NativeCombinerConnection {
    const conn = new NativeCombinerConnection(inputs, quiet, { transportFactory: () => combiner.transport(), retry })
    conn.setToken("pi-test")
    return conn
}

describe("isConnectionRefused", () => {
    it("finds ECONNREFUSED in the cause chain, and nothing else", () => {
        expect(isConnectionRefused(refusedError())).toBe(true)
        expect(isConnectionRefused(new TypeError("fetch failed"))).toBe(false)
        expect(isConnectionRefused(new Error("connect timeout after 8000ms"))).toBe(false)
        expect(isConnectionRefused(undefined)).toBe(false)
    })
})

describe("connect retry", () => {
    it("retries refused connects and connects once the combiner listens", async () => {
        const combiner = new Combiner(2)
        const conn = connection(combiner)
        await conn.ensureConnected()
        expect(combiner.attempts).toBe(3)
        expect(conn.state).toBe("connected")
        await conn.reset("test done")
    })

    it("does not retry an error other than a refused connect", async () => {
        const combiner = new Combiner(0, true)
        const conn = connection(combiner)
        await expect(conn.ensureConnected()).rejects.toThrow(/cannot reach the combiner/)
        expect(combiner.attempts).toBe(1)
        expect(conn.state).toBe("failed")
    })

    it("gives up when the budget is spent", async () => {
        const combiner = new Combiner(Number.POSITIVE_INFINITY)
        const conn = connection(combiner, { budgetMs: 150, reconnectBudgetMs: 150, firstMs: 20, maxMs: 40 })
        const started = Date.now()
        await expect(conn.ensureConnected()).rejects.toThrow(/cannot reach the combiner/)
        expect(combiner.attempts).toBeGreaterThan(1)
        expect(Date.now() - started).toBeLessThan(1_000)
    })

    it("stops at once when the liveness check says the combiner is gone", async () => {
        const combiner = new Combiner(Number.POSITIVE_INFINITY)
        const conn = connection(combiner)
        let checks = 0
        conn.setLivenessCheck(async () => {
            checks++
            return "gone"
        })
        await expect(conn.ensureConnected()).rejects.toThrow(/cannot reach the combiner/)
        expect(combiner.attempts).toBe(1)
        expect(checks).toBe(1)
    })

    it("keeps retrying when the liveness check itself fails", async () => {
        const combiner = new Combiner(2)
        const conn = connection(combiner)
        conn.setLivenessCheck(() => Promise.reject(new Error("sharedserver check crashed")))
        await conn.ensureConnected()
        expect(combiner.attempts).toBe(3)
        await conn.reset("test done")
    })

    it("a reset during the retry stops it", async () => {
        const combiner = new Combiner(Number.POSITIVE_INFINITY)
        const conn = connection(combiner, { budgetMs: 5_000, reconnectBudgetMs: 5_000, firstMs: 50, maxMs: 50 })
        const pending = conn.ensureConnected()
        pending.catch(() => undefined)
        await new Promise((r) => setTimeout(r, 10)) // first attempt refused, now backing off
        await conn.reset("session switched")
        const attemptsAtReset = combiner.attempts
        await expect(pending).rejects.toThrow()
        await new Promise((r) => setTimeout(r, 150))
        expect(combiner.attempts).toBe(attemptsAtReset)
    })
})

describe("liveness: relaunched", () => {
    it("a respawn during a reconnect restores the cold-start budget", async () => {
        const combiner = new Combiner()
        // A 60 ms reconnect budget alone would give up long before the 10 refusals end.
        const conn = connection(combiner, { budgetMs: 5_000, reconnectBudgetMs: 60, firstMs: 20, maxMs: 20 })
        await conn.ensureConnected()
        await conn.reset("combiner died")
        combiner.refuse = 10 // the respawned combiner takes a while to listen
        let respawned = false
        conn.setLivenessCheck(async () => {
            if (respawned) return "alive"
            respawned = true
            return "relaunched"
        })
        await conn.ensureConnected()
        expect(respawned).toBe(true)
        expect(conn.state).toBe("connected")
        await conn.reset("test done")
    })
})

describe("reconnect budget", () => {
    it("after a first successful connect, a refused reconnect gives up on the short budget", async () => {
        const combiner = new Combiner()
        const conn = connection(combiner, { budgetMs: 5_000, reconnectBudgetMs: 100, firstMs: 20, maxMs: 20 })
        await conn.ensureConnected()
        await conn.reset("combiner restarted")
        combiner.refuse = Number.POSITIVE_INFINITY
        const started = Date.now()
        await expect(conn.ensureConnected()).rejects.toThrow(/cannot reach the combiner/)
        // The 5 s first-connect budget would still be retrying.
        expect(Date.now() - started).toBeLessThan(1_000)
    })
})

describe("start gate", () => {
    it("no connect starts until the gate opens", async () => {
        const combiner = new Combiner()
        const conn = connection(combiner)
        let open: () => void = () => undefined
        conn.setStartGate(
            new Promise<void>((resolve) => {
                open = resolve
            }),
        )
        const pending = conn.ensureConnected()
        await new Promise((r) => setTimeout(r, 30))
        expect(combiner.attempts).toBe(0)
        open()
        await pending
        expect(combiner.attempts).toBe(1)
        await conn.reset("test done")
    })

    it("a reset while waiting at the gate: no connect for the old token", async () => {
        const combiner = new Combiner()
        const conn = connection(combiner)
        let open: () => void = () => undefined
        conn.setStartGate(
            new Promise<void>((resolve) => {
                open = resolve
            }),
        )
        const pending = conn.ensureConnected()
        pending.catch(() => undefined)
        conn.setToken("pi-other-session") // resets
        open()
        await expect(pending).rejects.toThrow(/superseded/)
        expect(combiner.attempts).toBe(0)
    })
})

const fastAuto: AutoReconnect = { dropMs: 20, dropMaxMs: 80, stableMs: 1_000, failMs: 20, failMaxMs: 40 }
const noRetry: ConnectRetry = { budgetMs: 0, reconnectBudgetMs: 0, firstMs: 10, maxMs: 10 }

function autoConnection(combiner: Combiner, log: LogFn = quiet): NativeCombinerConnection {
    const conn = new NativeCombinerConnection(inputs, log, {
        transportFactory: () => combiner.transport(),
        retry: noRetry,
        autoReconnect: fastAuto,
    })
    conn.setToken("pi-test")
    conn.setAutoReconnect(true)
    return conn
}

const until = async (cond: () => boolean, ms = 2_000): Promise<void> => {
    const t0 = Date.now()
    while (!cond()) {
        if (Date.now() - t0 > ms) throw new Error("timed out waiting")
        await new Promise((r) => setTimeout(r, 5))
    }
}

describe("auto-reconnect", () => {
    it("reconnects on its own after a dropped connection", async () => {
        const combiner = new Combiner()
        const conn = autoConnection(combiner)
        await conn.ensureConnected()
        await combiner.dropAll()
        await until(() => combiner.attempts === 2 && conn.state === "connected")
        await conn.close("test done")
    })

    it("off unless turned on: a drop just leaves it disconnected", async () => {
        const combiner = new Combiner()
        const conn = connection(combiner)
        await conn.ensureConnected()
        await combiner.dropAll()
        await new Promise((r) => setTimeout(r, 100))
        expect(combiner.attempts).toBe(1)
        expect(conn.state).toBe("disconnected")
    })

    it("retries a failed connect in the background, warning once per outage", async () => {
        const combiner = new Combiner(4) // refused 4 times, then listening
        const warnings: string[] = []
        const conn = autoConnection(combiner, (level, message) => {
            if (level === "warn") warnings.push(message)
        })
        await expect(conn.ensureConnected()).rejects.toThrow()
        await until(() => conn.state === "connected")
        expect(combiner.attempts).toBe(5)
        expect(warnings.filter((w) => /connect failed/.test(w)).length).toBe(1)
        await conn.close("test done")
    })

    it("close() stops background retries for good", async () => {
        const combiner = new Combiner(Number.POSITIVE_INFINITY)
        const conn = autoConnection(combiner)
        await expect(conn.ensureConnected()).rejects.toThrow()
        await conn.close("session shutdown")
        const attempts = combiner.attempts
        await new Promise((r) => setTimeout(r, 150))
        expect(combiner.attempts).toBe(attempts)
    })

    it("reconnect() leaves a connect in progress alone", async () => {
        const combiner = new Combiner()
        const conn = autoConnection(combiner)
        let open: () => void = () => undefined
        conn.setStartGate(
            new Promise<void>((resolve) => {
                open = resolve
            }),
        )
        const pending = conn.ensureConnected() // "connecting", parked at the gate
        conn.reconnect("the combiner process exited")
        open()
        await pending // not superseded by a reset
        expect(combiner.attempts).toBe(1)
        await conn.close("test done")
    })
})

describe("final review fixes (connection)", () => {
    it("after close(), ensureConnected refuses (no zombie connect)", async () => {
        const combiner = new Combiner()
        const conn = autoConnection(combiner)
        await conn.close("session shutdown")
        await expect(conn.ensureConnected()).rejects.toThrow(/closed/)
        expect(combiner.attempts).toBe(0)
        conn.reopen() // a host reusing the instance
        await conn.ensureConnected()
        expect(combiner.attempts).toBe(1)
        await conn.close("test done")
    })

    it("the 'not listening, retrying' note appears once per outage, not per background retry", async () => {
        const combiner = new Combiner(Number.POSITIVE_INFINITY)
        const infos: string[] = []
        const conn = new NativeCombinerConnection(
            inputs,
            (level, message) => {
                if (level === "info") infos.push(message)
            },
            {
                transportFactory: () => combiner.transport(),
                retry: { budgetMs: 60, reconnectBudgetMs: 60, firstMs: 10, maxMs: 10 },
                autoReconnect: fastAuto,
            },
        )
        conn.setToken("pi-test")
        conn.setAutoReconnect(true)
        await expect(conn.ensureConnected()).rejects.toThrow()
        await until(() => combiner.attempts > 15)
        expect(infos.filter((m) => /not listening/.test(m)).length).toBe(1)
        await conn.close("test done")
    })

    it("rebind to another combiner starts a fresh outage record (its failure is reported)", async () => {
        const combiner = new Combiner(Number.POSITIVE_INFINITY)
        const warnings: string[] = []
        const conn = autoConnection(combiner, (level, message) => {
            if (level === "warn") warnings.push(message)
        })
        await expect(conn.ensureConnected()).rejects.toThrow()
        conn.rebind({ baseUrl: "http://127.0.0.1:9799/mcp" })
        await expect(conn.ensureConnected()).rejects.toThrow()
        expect(warnings.filter((w) => /connect failed/.test(w)).length).toBe(2)
        await conn.close("test done")
    })
})
