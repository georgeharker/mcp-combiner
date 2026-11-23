--- mcp-companion.nvim — per-chat tool groups for CodeCompanion HTTP-adapter chats.
---
--- A chat's tools come from the combiner's view of THAT chat (design
--- 01M84R505D): its per-chat client (chat._mcp_client, the chat's token) lists
--- its tools, which already reflect the chat's server toggles and collections.
--- A server in that list that CodeCompanion's (global) MCP registry doesn't
--- have yet — one only some chats see, e.g. a collection turned on for this
--- chat — is registered there once under its `mcp:<server>` group. Then the
--- chat's groups are brought in line:
---
---   * a server the chat can no longer see loses its group in this chat;
---   * a server that became visible since the last sync gains its group;
---   * the aggregate `mcp:combiner` group, when the chat has it, is swapped
---     for per-server groups once the chat's view differs from the registry's.
---
--- Nothing is ever unregistered globally: other chats keep what they see. A
--- group a chat doesn't have can still be @-mentioned, but its calls go
--- through the chat's own client, so the combiner refuses them for that chat.
--- ACP chats list their tools themselves and are skipped.
--- @module mcp_companion.cc.chat_tools

local M = {}

local log = require("mcp_companion.log")

--- Group a tools/list result by server, using the combiner's server names
--- (longest first, so "basic-memory_x" isn't read as server "basic").
--- @param tools table[] MCP tool definitions
--- @param known string[] server names, longest first
--- @return table<string, table> server name -> { name, tools = {...} }
function M.by_server(tools, known)
    local out = {}
    for _, tool in ipairs(tools or {}) do
        local name = tool.name or ""
        for _, srv in ipairs(known) do
            local prefix = srv .. "_"
            if srv ~= "combiner" and name:sub(1, #prefix) == prefix then
                out[srv] = out[srv] or { name = srv, tools = {} }
                table.insert(
                    out[srv].tools,
                    vim.tbl_extend("force", tool, { _namespaced = name, _display = name:sub(#prefix + 1) })
                )
                break
            end
        end
    end
    return out
end

--- The group changes that bring a chat in line with what the combiner shows it.
--- Pure, for testing: no CodeCompanion calls.
---
--- A group is removed only when the combiner says its server is OFF for this
--- chat (the chat's blocklist or its collections) — never merely because the
--- server is missing from the chat's tool list: a server still warming up
--- (OAuth, a slow stdio start) is missing too, and must keep its group.
--- @param current table<string, any> the chat's groups (name -> anything)
--- @param visible table<string, boolean> servers in the chat's tool list now
--- @param previous table<string, boolean>|nil the list at the last sync (nil: first sync)
--- @param off table<string, boolean> servers the combiner has off for this chat
--- @param aggregate_servers table<string, boolean> servers the aggregate group covers
--- @param prefix string the group-name prefix (e.g. "mcp:")
--- @return string[] add, string[] remove
function M.plan(current, visible, previous, off, aggregate_servers, prefix)
    local add, remove = {}, {}
    local aggregate = prefix .. "combiner"
    local has_aggregate = current[aggregate] ~= nil
    local function want(group)
        if not current[group] and not vim.tbl_contains(add, group) then
            table.insert(add, group)
        end
    end

    -- The aggregate group stands for "every server": once one of its servers
    -- is off for this chat, it becomes per-server groups for the rest.
    if has_aggregate then
        local any_off = false
        for srv in pairs(aggregate_servers) do
            if off[srv] then
                any_off = true
            end
        end
        if any_off then
            table.insert(remove, aggregate)
            has_aggregate = false
            for srv in pairs(aggregate_servers) do
                if not off[srv] then
                    want(prefix .. srv)
                end
            end
        end
    end

    for srv in pairs(visible) do
        if not off[srv] then
            if has_aggregate then
                -- "Everything" includes servers the aggregate doesn't cover
                -- (e.g. a collection turned on just for this chat).
                if not aggregate_servers[srv] then
                    want(prefix .. srv)
                end
            elseif previous ~= nil and not previous[srv] then
                want(prefix .. srv)
            end
        end
    end

    for group in pairs(current) do
        if group ~= aggregate and group:sub(1, #prefix) == prefix and off[group:sub(#prefix + 1)] then
            table.insert(remove, group)
        end
    end
    table.sort(add)
    table.sort(remove)
    return add, remove
end

--- Re-list the chat's tools through its own client and align its groups.
--- @param chat table CodeCompanion chat
--- @param done? fun(err: string|nil, visible: table<string, boolean>|nil)
function M.sync(chat, done)
    done = done or function() end
    if not chat or not chat.tool_registry then
        done("no chat", nil)
        return
    end
    if chat.adapter and chat.adapter.type == "acp" then
        done(nil, nil) -- the agent lists its own tools
        return
    end
    local client = chat._mcp_client
    if not client then
        done("no per-chat client", nil)
        return
    end

    client:request_all("tools/list", "tools", function(err, tools)
        vim.schedule(function()
            if err or type(tools) ~= "table" then
                done("tools/list failed: " .. tostring(err), nil)
                return
            end
            local ok, cc_mcp = pcall(require, "codecompanion.mcp")
            if not ok then
                done("codecompanion.mcp not available", nil)
                return
            end
            local combiner = require("mcp_companion.combiner")
            local known = (combiner.client and combiner.client._known_server_names) or {}
            local servers = M.by_server(tools, known)

            -- Register what only some chats see (once, globally).
            local cc_tools = require("mcp_companion.cc.tools")
            local project = require("mcp_companion.project")
            local sysp =
                project.resolve_tool_system_prompts(require("mcp_companion.config").get().cc.tool_system_prompts)
            local added_any = false
            for name, info in pairs(servers) do
                if cc_mcp.get_tool_count(name) == 0 then
                    cc_tools.register_server(info, combiner.client, sysp)
                    added_any = true
                    log.info("CC: registered '%s' for chats that see it", name)
                end
            end
            if added_any and chat.tools and chat.tools.refresh then
                chat.tools:refresh({ adapter = chat.adapter })
            end

            local visible = {}
            for name in pairs(servers) do
                visible[name] = true
            end
            -- The aggregate group was built from the control connection's view.
            local aggregate_servers = {}
            for _, srv in ipairs(require("mcp_companion.state").field("servers") or {}) do
                if srv.name ~= "_combiner" and #(srv.tools or {}) > 0 then
                    aggregate_servers[srv.name] = true
                end
            end

            M._off_for_chat(chat, function(off)
                M._apply(chat, cc_mcp, visible, off, aggregate_servers, done)
            end)
        end)
    end)
end

--- What the combiner has OFF for this chat: its blocklist plus the servers its
--- collections hide (the token route). Unknown (no token, route failed): empty,
--- so nothing is removed.
--- @param chat table
--- @param cb fun(off: table<string, boolean>)
function M._off_for_chat(chat, cb)
    local token = chat._mcp_token
    if not token then
        cb({})
        return
    end
    local sessions = require("mcp_companion.combiner.sessions")
    sessions.get_filter(token, function(err, data)
        if err then
            log.debug("CC: chat %s filter lookup failed (%s); keeping its groups", tostring(chat.bufnr), err)
            cb({})
            return
        end
        cb(sessions.disabled_set(data))
    end)
end

--- Apply the planned group changes to *chat*.
function M._apply(chat, cc_mcp, visible, off, aggregate_servers, done)
    local prefix = cc_mcp.tool_prefix()
    local add, remove =
        M.plan(chat.tool_registry.groups or {}, visible, chat._mcp_visible, off, aggregate_servers, prefix)
    for _, group in ipairs(remove) do
        pcall(chat.tool_registry.remove_group, chat.tool_registry, group)
    end
    for _, group in ipairs(add) do
        chat.tool_registry:add(group, { config = chat.tools and chat.tools.tools_config })
    end
    if (#add > 0 or #remove > 0) and chat.tools and chat.tools.refresh then
        chat.tools:refresh({ adapter = chat.adapter })
    end
    chat._mcp_visible = visible
    log.debug(
        "CC: chat %s tools synced (+%s -%s)",
        tostring(chat.bufnr),
        table.concat(add, ","),
        table.concat(remove, ",")
    )
    done(nil, visible)
end

return M
