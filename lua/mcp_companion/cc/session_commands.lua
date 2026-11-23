--- mcp-companion.nvim — Session-scoped server toggle slash command
--- Registers /mcp-session as a static CC slash command that lets the user
--- enable or disable individual MCP servers for the current chat session only.
---
--- Unlike combiner__disable_server (global), the per-session toggle is invisible
--- to other sessions and is automatically cleaned up when the session ends.
---
--- Usage: type /mcp-session in a CC chat buffer, then pick a server and action.
--- @module mcp_companion.cc.session_commands

local M = {}

local log = require("mcp_companion.log")

-- Track per-chat session-disabled state locally so the picker shows current status
-- without needing an extra round-trip to the combiner.
-- Format: { [bufnr] = { [server_name] = true } }
--- @type table<integer, table<string, boolean>>
local _session_state = {}

--- Return the set of servers disabled for a given chat buffer.
--- @param bufnr integer
--- @return table<string, boolean>
local function _get_disabled(bufnr)
    _session_state[bufnr] = _session_state[bufnr] or {}
    return _session_state[bufnr]
end

--- Bring the chat's CodeCompanion tool groups in line with what the combiner
--- shows THIS chat (cc/chat_tools.lua): its own client lists its tools; servers
--- it can't see lose their group here, ones it newly sees gain theirs. Nothing
--- is unregistered globally — other chats are unaffected.
--- @param chat table CC chat object
local function _sync_cc_tool_groups(chat)
    require("mcp_companion.cc.chat_tools").sync(chat, function(err)
        if err then
            log.debug("CC: per-chat tool sync skipped: %s", err)
        end
    end)
end

--- Call the session toggle for the given chat via the token filter endpoint.
--- Uses chat._mcp_token as the stable identifier for both ACP and HTTP adapter chats.
--- @param chat table CC chat object
--- @param tool_name string "combiner__session_disable_server" or "combiner__session_enable_server"
--- @param server_name string
--- @param callback fun(err?: string, msg?: string)
local function _call_session_tool(chat, tool_name, server_name, callback)
    local token = chat._mcp_token
    if token then
        local sessions = require("mcp_companion.combiner.sessions")
        local is_disabling = tool_name == "combiner__session_disable_server"
        local action_key = is_disabling and "disable" or "enable"

        sessions.set_filter(token, { [action_key] = server_name }, function(err, data)
            if err then
                callback("Combiner filter update failed: " .. err)
                return
            end
            if data.disabled_servers and chat.bufnr then
                _session_state[chat.bufnr] = sessions.disabled_set(data)
            end
            local action = is_disabling and "disabled" or "enabled"
            callback(nil, string.format("%s %s for this session", action, server_name))
        end)
        return
    end

    -- Fallback: no token — call combiner meta-tool via Neovim MCP client
    local combiner = require("mcp_companion.combiner")
    if not combiner.client then
        callback("Combiner not connected")
        return
    end

    local chat_id = chat and chat.bufnr and tostring(chat.bufnr) or nil

    combiner.client:call_tool(tool_name, { server_name = server_name, chat_id = chat_id }, function(err, result)
        vim.schedule(function()
            if err then
                callback(tostring(err))
                return
            end
            local text = result and result.content and result.content[1] and result.content[1].text
            if text then
                local ok, data = pcall(vim.json.decode, text)
                if ok and data and data.disabled_servers and chat and chat.bufnr then
                    local disabled_map = {}
                    for _, s in ipairs(data.disabled_servers) do
                        disabled_map[s] = true
                    end
                    _session_state[chat.bufnr] = disabled_map
                end
            end
            local is_disabling = tool_name == "combiner__session_disable_server"
            local action = is_disabling and "disabled" or "enabled"
            callback(nil, string.format("%s %s for this session", action, server_name))
        end)
    end)
end

--- Toggle a server's session-visibility for *chat* and synchronise downstream
--- state (local cache, CC tool registry, status window).
---
--- Callable from any surface — currently used by the `/mcp-session` slash
--- command and the `S` keymap in :MCPStatus.
---
--- @param chat table CodeCompanion chat object
--- @param server_name string
--- @param done? fun(err: string|nil, info: { action: "enabled"|"disabled", server: string }|nil)
function M.toggle_server_for_session(chat, server_name, done)
    done = done or function() end
    if not chat or not chat.bufnr then
        done("no chat available", nil)
        return
    end

    local disabled = _get_disabled(chat.bufnr)
    local currently_disabled = disabled[server_name] == true
    local tool = currently_disabled and "combiner__session_enable_server" or "combiner__session_disable_server"
    local new_state = not currently_disabled -- true = will be disabled

    log.debug("Session toggle: %s %s", currently_disabled and "enabling" or "disabling", server_name)

    _call_session_tool(chat, tool, server_name, function(err, msg)
        if err then
            done(err, nil)
            return
        end

        -- Mirror the new state locally so the picker / UI reflect it
        -- without an extra round-trip.
        if new_state then
            disabled[server_name] = true
        else
            disabled[server_name] = nil
        end

        -- Bring the chat's tool groups in line with its new view.
        _sync_cc_tool_groups(chat)

        -- Refresh the status window if it's open (it shows session state).
        local ui_ok, ui = pcall(require, "mcp_companion.ui")
        if ui_ok and ui.is_open() then
            ui.render()
        end

        local action_label = new_state and "disabled" or "enabled"
        log.info("Session toggle: %s %s — %s", action_label, server_name, msg)
        done(nil, { action = action_label, server = server_name })
    end)
end

--- List the collections for *chat*: on/off for it and for the combiner, and
--- their servers (combiner__collections, naming the chat by its token).
--- @param chat table CC chat object
--- @param done fun(err: string|nil, collections: table<string, table>|nil)
function M.list_collections(chat, done)
    local combiner = require("mcp_companion.combiner")
    if not combiner.client then
        done("Combiner not connected", nil)
        return
    end
    local chat_id = chat and (chat._mcp_token or (chat.bufnr and tostring(chat.bufnr))) or nil
    combiner.client:call_tool("combiner__collections", { chat_id = chat_id }, function(err, result)
        vim.schedule(function()
            if err then
                done(tostring(err), nil)
                return
            end
            local text = result and result.content and result.content[1] and result.content[1].text
            local ok, data = pcall(vim.json.decode, text or "")
            if not ok or type(data) ~= "table" then
                done(text or "Empty response", nil)
                return
            end
            done(nil, data)
        end)
    end)
end

--- Turn a collection on or off for *chat* only, or back to following the
--- combiner (`on == nil`), then refresh the chat's server state and tools.
--- @param chat table CC chat object
--- @param name string collection name
--- @param on boolean|nil true = on here, false = off here, nil = follow the combiner
--- @param done? fun(err: string|nil, info: table|nil)
function M.set_collection_for_session(chat, name, on, done)
    done = done or function() end
    if not chat or not chat.bufnr then
        done("no chat available", nil)
        return
    end
    local function after(err, data)
        if err then
            done(err, nil)
            return
        end
        -- The chat's server view changed: refresh the picker state and tools.
        M.fetch_session_status(chat, function()
            _sync_cc_tool_groups(chat)
            local ui_ok, ui = pcall(require, "mcp_companion.ui")
            if ui_ok and ui.is_open() then
                ui.render()
            end
            done(nil, data)
        end)
    end

    local token = chat._mcp_token
    if token then
        require("mcp_companion.combiner.sessions").set_collection(token, name, on, after)
        return
    end
    local combiner = require("mcp_companion.combiner")
    if not combiner.client then
        done("Combiner not connected", nil)
        return
    end
    local tool = on == true and "combiner__session_enable_collection"
        or on == false and "combiner__session_disable_collection"
        or "combiner__session_reset_collection"
    combiner.client:call_tool(tool, { collection = name, chat_id = tostring(chat.bufnr) }, function(err, result)
        vim.schedule(function()
            if err then
                after(tostring(err), nil)
                return
            end
            local text = result and result.content and result.content[1] and result.content[1].text or ""
            if text:sub(1, 6) == "Error:" then
                after(text, nil)
                return
            end
            after(nil, { text = text })
        end)
    end)
end

--- The /mcp-collection picker: choose a collection, then on / off / follow
--- the combiner for this chat.
--- @param chat table CC chat object
function M.pick_collection(chat)
    M.list_collections(chat, function(err, collections)
        if err then
            vim.notify("mcp-companion: collections unavailable: " .. err, vim.log.levels.ERROR)
            return
        end
        local names = vim.tbl_keys(collections or {})
        table.sort(names)
        if #names == 0 then
            vim.notify("mcp-companion: no collections are configured on this machine", vim.log.levels.WARN)
            return
        end
        local items = {}
        for _, name in ipairs(names) do
            local c = collections[name]
            local servers = vim.tbl_keys(c.servers or {})
            table.sort(servers)
            table.insert(items, {
                name = name,
                label = string.format(
                    "[%s] %s — %s; combiner %s (%s)",
                    c.this_chat and " ON" or "OFF",
                    name,
                    c.this_chat_set and "set for this chat" or "following the combiner",
                    c.enabled and "on" or "off",
                    table.concat(servers, ", ")
                ),
            })
        end
        vim.ui.select(items, {
            prompt = "Collection for this chat:",
            format_item = function(item)
                return item.label
            end,
        }, function(choice)
            if not choice then
                return
            end
            local actions = {
                { label = "On for this chat", on = true },
                { label = "Off for this chat", on = false },
                { label = "Follow the combiner", on = nil, reset = true },
            }
            vim.ui.select(actions, {
                prompt = string.format("'%s' in this chat:", choice.name),
                format_item = function(a)
                    return a.label
                end,
            }, function(action)
                if not action then
                    return
                end
                M.set_collection_for_session(chat, choice.name, action.on, function(e)
                    if e then
                        vim.notify("mcp-companion: collection change failed: " .. e, vim.log.levels.ERROR)
                        return
                    end
                    vim.notify(
                        string.format("mcp-companion: '%s' %s", choice.name, action.label:lower()),
                        vim.log.levels.INFO
                    )
                end)
            end)
        end)
    end)
end

--- Register /mcp-session as a static CC slash command.
--- Called once at setup — not driven by combiner_ready/servers_updated.
function M.register()
    local cc_config_ok, cc_config = pcall(require, "codecompanion.config")
    if not cc_config_ok then
        log.debug("codecompanion.config not available, skipping /mcp-session registration")
        return
    end

    local slash_cmds = cc_config.interactions
        and cc_config.interactions.chat
        and cc_config.interactions.chat.slash_commands
    if not slash_cmds then
        log.debug("CC slash_commands table not found, skipping /mcp-session")
        return
    end

    slash_cmds["mcp-session"] = {
        description = "Enable or disable MCP servers for this chat session",
        ---@param chat table CodeCompanion chat object
        callback = function(chat)
            local state = require("mcp_companion.state")
            local servers = state.field("servers") or {}

            -- Filter out the internal _combiner pseudo-server
            local names = {}
            for _, srv in ipairs(servers) do
                if srv.name ~= "_combiner" then
                    table.insert(names, srv.name)
                end
            end

            if #names == 0 then
                vim.notify("mcp-companion: no MCP servers available", vim.log.levels.WARN)
                return
            end

            local bufnr = chat.bufnr
            local disabled = _get_disabled(bufnr)

            -- Build picker items with current status
            local items = {}
            for _, name in ipairs(names) do
                table.insert(items, {
                    name = name,
                    label = string.format("[%s] %s", disabled[name] and "OFF" or " ON", name),
                })
            end

            vim.ui.select(items, {
                prompt = "Toggle MCP server for this session:",
                format_item = function(item)
                    return item.label
                end,
            }, function(choice)
                if not choice then
                    return
                end
                M.toggle_server_for_session(chat, choice.name, function(err, info)
                    if err then
                        vim.notify(string.format("mcp-companion: session toggle failed: %s", err), vim.log.levels.ERROR)
                        return
                    end
                    vim.notify(
                        string.format("mcp-companion: %s %s for this session", info.action, info.server),
                        vim.log.levels.INFO
                    )
                end)
            end)
        end,
    }

    slash_cmds["mcp-collection"] = {
        description = "Turn a server collection on or off for this chat",
        ---@param chat table CodeCompanion chat object
        callback = function(chat)
            M.pick_collection(chat)
        end,
    }

    slash_cmds["mcp-session-save"] = {
        description = "Save current MCP server visibility as .mcp-companion.json",
        ---@param chat table CodeCompanion chat object
        callback = function(chat)
            -- Single-arg parsing: trailing token after the command name
            -- selects the format (shortest|allowed|disabled).  CC slash
            -- commands receive the full message line as chat.context, but to
            -- stay portable we just prompt when an arg isn't supplied.
            vim.ui.select({ "shortest", "allowed", "disabled" }, {
                prompt = "Project file format:",
            }, function(choice)
                if not choice then
                    return
                end
                local cc_init = require("mcp_companion.cc")
                cc_init._save_project_config_interactive(chat, choice)
            end)
        end,
    }

    log.debug("Registered /mcp-session, /mcp-collection and /mcp-session-save slash commands")
end

--- Return the session-disabled set for a chat buffer (read-only view).
--- @param bufnr integer
--- @return table<string, boolean> server_name->true for session-disabled servers
function M.get_session_state(bufnr)
    return _session_state[bufnr] or {}
end

--- Set the session-disabled state for a chat buffer.
--- Used by ACP filter initialization to sync state for MCPStatus display.
--- @param bufnr integer
--- @param disabled_map table<string, boolean> server_name->true for disabled servers
function M.set_session_state(bufnr, disabled_map)
    _session_state[bufnr] = disabled_map
end

--- Fetch session status from the combiner (async).
--- Uses chat._mcp_token as the stable identifier for both ACP and HTTP adapter chats.
--- @param chat table|nil CC chat object (nil for non-chat context)
--- @param callback fun(err: string|nil, disabled: table<string, boolean>|nil)
function M.fetch_session_status(chat, callback)
    local token = chat and chat._mcp_token
    if token then
        local sessions = require("mcp_companion.combiner.sessions")
        log.debug(
            "MCPStatus: fetching session filter via token (bufnr=%s token=%s)",
            tostring(chat and chat.bufnr),
            token
        )
        sessions.get_filter(token, function(err, data)
            if err then
                callback(err, nil)
                return
            end
            local disabled = sessions.disabled_set(data)
            if chat and chat.bufnr then
                _session_state[chat.bufnr] = disabled
            end
            callback(nil, disabled)
        end)
        return
    end

    -- Fallback: no token — call combiner meta-tool
    local combiner = require("mcp_companion.combiner")
    if not combiner.client then
        log.debug("MCPStatus: combiner not connected (bufnr=%s)", tostring(chat and chat.bufnr))
        callback("Combiner not connected", nil)
        return
    end

    local chat_id = chat and chat.bufnr and tostring(chat.bufnr) or nil
    combiner.client:call_tool("combiner__session_status", { chat_id = chat_id }, function(err, result)
        vim.schedule(function()
            if err then
                callback(tostring(err), nil)
                return
            end
            local text = result and result.content and result.content[1] and result.content[1].text
            if not text then
                callback("Empty response", nil)
                return
            end
            local ok, data = pcall(vim.json.decode, text)
            if not ok or not data then
                callback("JSON parse error", nil)
                return
            end
            local disabled = require("mcp_companion.combiner.sessions").disabled_set(data)
            if chat and chat.bufnr then
                _session_state[chat.bufnr] = disabled
            end
            callback(nil, disabled)
        end)
    end)
end

--- Clear session state for a chat buffer (call on chat close).
--- @param bufnr integer
function M.clear(bufnr)
    _session_state[bufnr] = nil
end

return M
