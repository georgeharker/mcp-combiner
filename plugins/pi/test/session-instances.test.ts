// One pi process can host several sessions: a chat and its subagents' child
// sessions. A new extension instance may only tear down an earlier instance of
// ITS OWN session (a reload that didn't shut it down), never another session's.
import { beforeEach, describe, expect, it } from "vitest"
import { claimSession } from "../src/index.js"

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
