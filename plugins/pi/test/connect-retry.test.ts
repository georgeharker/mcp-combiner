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
        return pair.client
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
            return false
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
