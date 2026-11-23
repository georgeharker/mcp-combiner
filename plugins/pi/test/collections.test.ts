import { describe, expect, it } from "vitest"

import { collectionCall, collectionsText } from "../src/client/control.js"
import type { CombinerConnection } from "../src/client/types.js"

type Call = { name: string; args: Record<string, unknown> | undefined }

function fakeConnection(reply: (call: Call) => unknown, token = "pi-chat-1") {
    const calls: Call[] = []
    const connection = {
        sessionToken: token,
        async callTool(name: string, args: Record<string, unknown> | undefined) {
            calls.push({ name, args })
            return { content: [{ text: JSON.stringify(reply({ name, args })) }] }
        },
    } as unknown as CombinerConnection
    return { connection, calls }
}

describe("collections verb", () => {
    it("lists each collection for this chat and the combiner, with its servers", async () => {
        const { connection, calls } = fakeConnection(() => ({
            music: {
                enabled: false,
                this_chat: true,
                this_chat_set: true,
                servers: { ableton: { running: true, this_chat: true } },
            },
            core: {
                enabled: true,
                this_chat: true,
                this_chat_set: false,
                servers: { github: { running: true, this_chat: true }, jira: { running: false, this_chat: false } },
            },
        }))
        const text = await collectionsText(connection)
        expect(calls).toEqual([{ name: "combiner__collections", args: { chat_id: "pi-chat-1" } }])
        expect(text).toContain("● core: on here (following the combiner; combiner: on)")
        expect(text).toContain("● music: on here (set for this chat; combiner: off)")
        expect(text).toContain("    ableton — visible here")
        expect(text).toContain("    jira — not running")
        // Alphabetical: core before music.
        expect(text.indexOf("core:")).toBeLessThan(text.indexOf("music:"))
    })

    it("says so when no collections are configured", async () => {
        const { connection } = fakeConnection(() => ({}))
        expect(await collectionsText(connection)).toBe("No collections are configured on this machine.")
    })
})

describe("collection on|off|reset", () => {
    it.each([
        ["on", "combiner__session_enable_collection"],
        ["off", "combiner__session_disable_collection"],
        ["reset", "combiner__session_reset_collection"],
    ] as const)("%s calls %s for this chat", async (action, tool) => {
        const { connection, calls } = fakeConnection(() => ({ this_chat: action === "on", servers: {} }))
        await collectionCall(connection, action, "music")
        expect(calls).toEqual([{ name: tool, args: { collection: "music", chat_id: "pi-chat-1" } }])
    })

    it("reports what changed", async () => {
        const { connection } = fakeConnection(() => ({
            this_chat: true,
            this_chat_set: true,
            servers: { ableton: true, live: false },
            started: ["ableton"],
            stopped: [],
            failed: [],
        }))
        expect(await collectionCall(connection, "on", "music")).toBe(
            "Collection 'music' on for this chat; visible here: ableton; started ableton.",
        )
    })

    it("passes the combiner's error through", async () => {
        const connection = {
            sessionToken: "pi-chat-1",
            async callTool() {
                return { content: [{ text: "Error: collection 'nope' isn't loaded here (loaded: music)" }] }
            },
        } as unknown as CombinerConnection
        expect(await collectionCall(connection, "on", "nope")).toBe(
            "Error: collection 'nope' isn't loaded here (loaded: music)",
        )
    })
})
