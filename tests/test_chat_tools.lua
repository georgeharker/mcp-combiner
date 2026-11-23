--- Tests for mcp_companion.cc.chat_tools (per-chat tool groups) and the
--- collection-aware disabled set in mcp_companion.combiner.sessions.
---
--- Run from the repo root:
---   scripts/test-lua.sh test_chat_tools

local function script_dir()
    local info = debug.getinfo(1, "S")
    local src = info.source:sub(1, 1) == "@" and info.source:sub(2) or info.source
    return vim.fn.fnamemodify(src, ":p:h")
end
local repo_root = vim.fn.fnamemodify(script_dir(), ":h")
package.path = repo_root .. "/lua/?.lua;" .. repo_root .. "/lua/?/init.lua;" .. package.path

package.loaded["mcp_companion.log"] = {
    debug = function() end,
    info = function() end,
    warn = function() end,
    error = function() end,
}

local chat_tools = require("mcp_companion.cc.chat_tools")
local sessions = require("mcp_companion.combiner.sessions")

local passed, failed = 0, 0

local function test(name, fn)
    local ok, err = pcall(fn)
    if ok then
        passed = passed + 1
        print(string.format("  PASS: %s", name))
    else
        failed = failed + 1
        print(string.format("  FAIL: %s — %s", name, err))
    end
end

local function assert_eq(actual, expected, msg)
    if not vim.deep_equal(actual, expected) then
        error(string.format("%s: expected %s, got %s", msg or "", vim.inspect(expected), vim.inspect(actual)))
    end
end

local function set(list)
    local out = {}
    for _, v in ipairs(list) do
        out[v] = true
    end
    return out
end

print("=== chat_tools.by_server ===")

test("groups tools by the longest matching server name", function()
    local tools = {
        { name = "basic-memory_write_note" },
        { name = "basic_echo" },
        { name = "combiner__status" },
        { name = "stranger_thing" },
    }
    local out = chat_tools.by_server(tools, { "basic-memory", "combiner", "basic" })
    assert_eq(vim.tbl_keys(out), vim.tbl_keys({ ["basic-memory"] = 1, basic = 1 }), "servers")
    assert_eq(out["basic-memory"].tools[1]._display, "write_note", "display name")
    assert_eq(out["basic-memory"].tools[1]._namespaced, "basic-memory_write_note", "namespaced name")
    assert_eq(out.basic.tools[1]._display, "echo", "short server")
end)

print("=== chat_tools.plan ===")

local P = "mcp:"
local NONE = {}

test("a server missing from the list (still warming up) keeps its group", function()
    local current = { ["mcp:github"] = {}, ["mcp:slow-oauth"] = {} }
    local add, remove = chat_tools.plan(current, set({ "github" }), nil, NONE, NONE, P)
    assert_eq(add, {}, "nothing added on a first sync")
    assert_eq(remove, {}, "nothing removed: slow-oauth isn't off, just not listed yet")
end)

test("a server the combiner has off for this chat loses its group", function()
    local current = { ["mcp:github"] = {}, ["mcp:ableton"] = {} }
    local _, remove = chat_tools.plan(current, set({ "github" }), nil, set({ "ableton" }), NONE, P)
    assert_eq(remove, { "mcp:ableton" }, "removed")
end)

test("a server newly visible gains its group", function()
    local current = { ["mcp:github"] = {} }
    local add, remove = chat_tools.plan(current, set({ "github", "ableton" }), set({ "github" }), NONE, NONE, P)
    assert_eq(add, { "mcp:ableton" }, "added")
    assert_eq(remove, {}, "nothing removed")
end)

test("a visible server the user never added stays out", function()
    local add = chat_tools.plan({}, set({ "github" }), set({ "github" }), NONE, NONE, P)
    assert_eq(add, {}, "no forced groups")
end)

test("the aggregate stays while none of its servers is off", function()
    local current = { ["mcp:combiner"] = {} }
    local agg = set({ "github", "jira" })
    local add, remove = chat_tools.plan(current, set({ "github" }), nil, NONE, agg, P)
    assert_eq(add, {}, "no per-server groups (jira just isn't listed yet)")
    assert_eq(remove, {}, "aggregate kept")
end)

test("the aggregate becomes per-server groups once one of its servers is off", function()
    local current = { ["mcp:combiner"] = {} }
    local agg = set({ "github", "jira" })
    local add, remove = chat_tools.plan(current, set({ "github" }), nil, set({ "jira" }), agg, P)
    assert_eq(add, { "mcp:github" }, "per-server groups for the rest")
    assert_eq(remove, { "mcp:combiner" }, "aggregate dropped")
end)

test("an aggregate chat also gets servers the aggregate doesn't cover", function()
    local current = { ["mcp:combiner"] = {} }
    local add, remove = chat_tools.plan(current, set({ "github", "ableton" }), nil, NONE, set({ "github" }), P)
    assert_eq(add, { "mcp:ableton" }, "collection turned on just for this chat")
    assert_eq(remove, {}, "aggregate kept")
end)

test("non-MCP groups are left alone", function()
    local current = { ["files"] = {}, ["mcp:gone"] = {} }
    local _, remove = chat_tools.plan(current, set({}), nil, set({ "gone", "files" }), NONE, P)
    assert_eq(remove, { "mcp:gone" }, "only mcp groups")
end)

print("=== sessions.disabled_set ===")

test("off servers include the ones the chat's collections hide", function()
    local out = sessions.disabled_set({ disabled_servers = { "github" }, hidden_by_collections = { "ableton" } })
    assert_eq(out, { github = true, ableton = true }, "union")
    assert_eq(sessions.disabled_set(nil), {}, "nil data")
end)

print(string.format("\n=== %d passed, %d failed ===", passed, failed))
if failed > 0 then
    os.exit(1)
end
