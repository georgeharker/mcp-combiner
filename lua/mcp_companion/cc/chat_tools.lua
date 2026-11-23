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

--- The group changes that bring a chat in line with what it can see.
--- Pure, for testing: no CodeCompanion calls.
--- @param current table<string, any> the chat's groups (name -> anything)
--- @param visible table<string, boolean> servers the chat can see now
--- @param previous table<string, boolean>|nil what it could see at the last sync
--- @param registered table<string, boolean> servers with a global group
--- @param prefix string the group-name prefix (e.g. "mcp:")
--- @return string[] add, string[] remove
function M.plan(current, visible, previous, registered, prefix)
    local add, remove = {}, {}
    local aggregate = prefix .. "combiner"
    local has_aggregate = current[aggregate] ~= nil

    -- The aggregate group stands for "every server": keep it only while this
    -- chat sees exactly what the registry has.
    local same_as_registry = true
    for srv in pairs(registered) do
        if not visible[srv] then
            same_as_registry = false
        end
    end
    for srv in pairs(visible) do
        if not registered[srv] then
            same_as_registry = false
        end
    end
    if has_aggregate and not same_as_registry then
        table.insert(remove, aggregate)
        for srv in pairs(visible) do
            if not current[prefix .. srv] then
                table.insert(add, prefix .. srv)
            end
        end
    else
        for srv in pairs(visible) do
            local group = prefix .. srv
            local newly = previous ~= nil and not previous[srv]
            if newly and not current[group] and not has_aggregate then
                table.insert(add, group)
            end
        end
    end

    for group in pairs(current) do
        if group ~= aggregate and group:sub(1, #prefix) == prefix then
            local srv = group:sub(#prefix + 1)
            if not visible[srv] then
                table.insert(remove, group)
            end
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

            local visible, registered = {}, {}
            for name in pairs(servers) do
                visible[name] = true
            end
            local _, groups = cc_mcp.get_registered_tools()
            local prefix = cc_mcp.tool_prefix()
            for group in pairs(groups or {}) do
                local srv = group:sub(#prefix + 1)
                if group:sub(1, #prefix) == prefix and srv ~= "combiner" then
                    registered[srv] = true
                end
            end

            local add, remove = M.plan(chat.tool_registry.groups or {}, visible, chat._mcp_visible, registered, prefix)
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
        end)
    end)
end

return M
