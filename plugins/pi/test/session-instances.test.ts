// One pi process can host several sessions: a chat and its subagents' child
// sessions. A new extension instance may only tear down an earlier instance of
// ITS OWN session (a reload that didn't shut it down), never another session's.
import { beforeEach, describe, expect, it } from "vitest"
import { claimSession, releaseSession, sessionEnded, sessionStarted, type ProcessSessions } from "../src/index.js"

describe("claimSession", () => {
    beforeEach(() => {
        delete (globalThis as Record<string, unknown>).__piMcpCombinerInstances
    })

    it("a subagent's session never disposes its parent's instance", () => {
        let parentClosed = false
        claimSession("parent", () => (parentClosed = true))
        claimSession("child", () => undefined)
        expect(parentClosed).toBe(false)
    })

    it("a reload of the same session disposes the earlier instance", () => {
        let oldClosed = false
        claimSession("chat", () => (oldClosed = true))
        claimSession("chat", () => undefined)
        expect(oldClosed).toBe(true)
    })

    it("claiming again with the same instance is a no-op", () => {
        let closed = 0
        const dispose = () => void closed++
        claimSession("chat", dispose)
        claimSession("chat", dispose)
        expect(closed).toBe(0)
    })

    it("without a session id nothing is disposed", () => {
        let closed = false
        claimSession("chat", () => (closed = true))
        claimSession(undefined, () => undefined)
        expect(closed).toBe(false)
    })
})

describe("releaseSession", () => {
    it("forgets the session's instance, but not a newer one that took the slot", () => {
        const g = globalThis as Record<string, unknown>
        delete g.__piMcpCombinerInstances
        const first = () => undefined
        const second = () => undefined
        claimSession("chat", first)
        claimSession("chat", second)
        releaseSession("chat", first) // stale: the slot is second's
        let closed = false
        claimSession("chat", () => (closed = true)) // disposes second
        expect(closed).toBe(false)
        const map = g.__piMcpCombinerInstances as Map<string, unknown>
        releaseSession("chat", [...map.values()][0] as () => void)
        expect(map.size).toBe(0)
    })
})

describe("process sessions: when the combiner ref is released", () => {
    const fresh = (): ProcessSessions => ({ live: new Set(), pending: 0 })

    it("a subagent quitting keeps it; the last quit releases it", () => {
        const st = fresh()
        sessionStarted(st, "parent", "startup")
        sessionStarted(st, "child", "startup")
        expect(sessionEnded(st, "child", "quit")).toBe(false)
        expect(sessionEnded(st, "parent", "quit")).toBe(true)
    })

    it("/new then quit releases it (the old session doesn't linger)", () => {
        const st = fresh()
        sessionStarted(st, "a", "startup")
        expect(sessionEnded(st, "a", "new")).toBe(false)
        sessionStarted(st, "a2", "new")
        expect(sessionEnded(st, "a2", "quit")).toBe(true)
    })

    it("a child quitting while the parent reloads keeps it", () => {
        const st = fresh()
        sessionStarted(st, "parent", "startup")
        sessionStarted(st, "child", "startup")
        expect(sessionEnded(st, "parent", "reload")).toBe(false)
        expect(sessionEnded(st, "child", "quit")).toBe(false) // parent's replacement pending
        sessionStarted(st, "parent", "reload")
        expect(sessionEnded(st, "parent", "quit")).toBe(true)
    })
})
