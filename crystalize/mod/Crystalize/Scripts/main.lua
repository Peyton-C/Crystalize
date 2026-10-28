-- Crystalize: runs the Crystalize MCP server's commands inside FUSER.
--
-- The server writes ipc/request.lua, a Lua table literal, and this mod answers with
-- ipc/response-<id>.json. UE4SS's Lua has no sockets and FUSER runs under Wine, so a
-- shared folder is the channel; a table literal means load() replaces a JSON parser.
--
-- Anything touching a game object runs through ExecuteInGameThread, which rides
-- UE4SS's EngineTick hook. HookUObjectProcessEvent must stay off: with it on, a
-- Blueprint event during the startup logos returns garbage and FUSER crashes on it.

local VERSION = "0.1.0"
local POLL_MS = 50

-- EActionAfterDiscSpawn
local PLAY, QUEUE = 1, 2

-- ECalibratedTimeType: the clock as the audio plays it, with no latency calibration
local RAW = 0

-- FTransposes' fields, in EKey order
local KEY_NAMES = { "C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B" }

-- A new value each time the mod loads, so the server notices FUSER restarting
local SESSION = string.format("%d-%d", os.time(), math.random(1, 1 << 30))

local ARRAY = {}
local NULL = setmetatable({}, { __tostring = function() return "null" end })

local function array(t)
    return setmetatable(t or {}, ARRAY)
end

local function log(fmt, ...)
    print(string.format("[Crystalize] " .. fmt .. "\n", ...))
end

------------------------------------------------------------------------------- JSON

local ESCAPES = {
    ['"'] = '\\"', ["\\"] = "\\\\", ["\b"] = "\\b", ["\f"] = "\\f",
    ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t",
}

local function encodeString(s)
    local escaped = s:gsub('[%c"\\]', function(c)
        return ESCAPES[c] or string.format("\\u%04x", c:byte())
    end)
    return '"' .. escaped .. '"'
end

local function encode(v)
    local t = type(v)
    if v == nil or v == NULL then
        return "null"
    elseif t == "boolean" then
        return tostring(v)
    elseif t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then
            return "null"
        end
        return math.type(v) == "integer" and tostring(v) or string.format("%.14g", v)
    elseif t == "string" then
        return encodeString(v)
    elseif t == "table" then
        local parts = {}
        -- An empty table is an object unless it was made with array()
        if getmetatable(v) == ARRAY or #v > 0 then
            for i = 1, #v do
                parts[i] = encode(v[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        for k, item in pairs(v) do
            parts[#parts + 1] = encodeString(tostring(k)) .. ":" .. encode(item)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    error("cannot encode a " .. t, 0)
end

------------------------------------------------------------------------------- game

-- UE4SS hands back an invalid object, not nil, for a null property or a failed find
local function valid(obj)
    return obj ~= nil and obj:IsValid()
end

local function first(class)
    local obj = FindFirstOf(class)
    if not valid(obj) then
        error(class .. " not found, open a freestyle session or a campaign set first", 0)
    end
    return obj
end

local function text(ftext)
    local ok, s = pcall(function() return ftext:ToString() end)
    return ok and s or NULL
end

-- The four BP_Deck_C actors by ETrackType, 0 to 3
local function decks()
    local found = {}
    for _, deck in ipairs(FindAllOf("BP_Deck_C") or {}) do
        if valid(deck) then
            found[deck.track] = deck
        end
    end
    return found
end

local function discName(disc)
    if not valid(disc) then
        return NULL
    end
    return disc:GetCelShortName():ToString()
end

-- Every disc FUSER has loaded, by short name. Freestyle loads the whole library, so a
-- miss means a stale cache or a name that does not exist, and one rebuild tells which.
local cels = {}

local function findCel(name)
    local cel = cels[name]
    if valid(cel) then
        return cel
    end
    cels = {}
    for _, c in ipairs(FindAllOf("SongCelData") or {}) do
        if valid(c) then
            cels[c.shortName:ToString()] = c
        end
    end
    cel = cels[name]
    if not cel then
        error("no disc named " .. tostring(name), 0)
    end
    return cel
end

local function checkDeck(deck)
    if math.type(deck) ~= "integer" or deck < 0 or deck > 3 then
        error("deck must be 0 to 3, got " .. tostring(deck), 0)
    end
    return deck
end

------------------------------------------------------------------------------- ops

local ops = {}

function ops.ping()
    return { version = VERSION }
end

function ops.state()
    local audio = first("HypeAudio")
    local state = {
        key = audio:GetKey(),
        mode = audio:GetKeyMode(),
        bpm = audio:GetCurrentBPM(),
        -- 90 to 157 as shipped; FuserSongLoader builds from May 2026 widen them to 60 to 200
        bpm_min = audio:GetMinBpm(),
        bpm_max = audio:GetMaxBpm(),
        decks = array(),
    }
    local found = decks()
    for track = 0, 3 do
        local deck = found[track]
        state.decks[track + 1] = deck and {
            playing = discName(deck.CurPlayingDisc),
            queued = discName(deck.CurQueuedDisc),
        } or NULL
    end
    return state
end

function ops.clock()
    local audio = first("HypeAudio")
    return {
        beat = audio:GetCurrentBeat(RAW),
        measure = audio:GetCurrentMeasure(RAW),
        tick = audio:GetCurrentTick(RAW),
        bpm = audio:GetCurrentBPM(),
    }
end

-- The semitones FUSER shifts a disc by in each session key, C to B
local function transposes(cel)
    local t = cel.Transposes
    local out = array()
    for i, name in ipairs(KEY_NAMES) do
        out[i] = t[name]
    end
    return out
end

-- Whether a disc has MIDI for a mode, which FUSER needs to play it in that mode;
-- NULL when the array cannot be read
local function hasAssets(cel, field)
    local ok, n = pcall(function() return cel[field]:GetArrayNum() end)
    if not ok then
        return NULL
    end
    return n > 0
end

function ops.discs()
    local out = array()
    -- FUSER loads every song, DLC included, and a disc from DLC the player does not own
    -- goes on a deck but stays silent. Asked once per song, since a song has four discs;
    -- keyed by name because UE4SS wraps the same object anew on every property read.
    local owned = {}
    local function isOwned(name, song)
        if owned[name] == nil then
            local ok, result = pcall(function() return song:IsOwnedAndInstalled() end)
            owned[name] = not ok or result ~= false
        end
        return owned[name]
    end
    for _, cel in ipairs(FindAllOf("SongCelData") or {}) do
        local ok, disc = pcall(function()
            local disc = {
                name = cel.shortName:ToString(),
                title = text(cel.Title),
                type = cel.CelType,
                key = cel.Key,
                mode = cel.mode,
                bpm = cel.BPM,
                instrument = cel.instrument,
                length_ticks = cel.SongLengthTicks,
            }
            -- Optional: a disc without them still plays, so a failure here costs only a check
            local ok_t, t = pcall(transposes, cel)
            disc.transposes = ok_t and t or NULL
            disc.has_major = hasAssets(cel, "MajorMidiSongAssets")
            disc.has_minor = hasAssets(cel, "MinorMidiSongAssets")
            local song = cel.song
            if valid(song) then
                disc.song = song.songShortName:ToString()
                disc.artist = text(song.artist)
                disc.genre = song.Genre
                disc.year = song.year
                disc.owned = isOwned(disc.song, song)
            end
            return disc
        end)
        if ok and valid(cel) and disc.name ~= "None" then
            out[#out + 1] = disc
        end
    end
    return out
end

function ops.drop(args)
    local cel = findCel(args.name)
    local deck = checkDeck(args.deck)
    -- The last three are a snapshot ("moment") and battle mode, which freestyle has neither of
    first("DeskState"):NetSpawnDisc(cel, args.queue and QUEUE or PLAY, deck, true, nil, 0, false)
    return { name = args.name, deck = deck, queued = args.queue == true }
end

function ops.eject(args)
    first("DeskState"):EjectTrack(checkDeck(args.deck), false)
    return { deck = args.deck }
end

function ops.drop_queue()
    first("DeskState"):DoDropQueue()
    return {}
end

function ops.set_key(args)
    local controller = first("AudioController")
    controller:NetSetKeyMode(args.mode)
    controller:NetSetKey(args.key)
    return { key = args.key, mode = args.mode }
end

function ops.set_tempo(args)
    first("AudioController"):NetSetTempo(args.bpm)
    return { bpm = args.bpm }
end

function ops.mute(args)
    first("AudioController"):NetMuteTrack(checkDeck(args.deck), args.on == true)
    return { deck = args.deck, on = args.on == true }
end

function ops.solo(args)
    first("AudioController"):NetSoloTrack(checkDeck(args.deck), args.on == true)
    return { deck = args.deck, on = args.on == true }
end

-- A number, boolean or string as is. Anything else, such as a struct a UFunction hands back,
-- would stop the response encoding, so it is reported as null
local function plain(v)
    local t = type(v)
    if t == "number" or t == "boolean" or t == "string" then
        return v
    end
    return NULL
end

-- A UFunction's answer, or null if calling it fails
local function ask(obj, method)
    local ok, v = pcall(function() return obj[method](obj) end)
    -- Not "ok and plain(v) or NULL", which turns an answer of false into null
    if ok then
        return plain(v)
    end
    return NULL
end

-- A TArray's items. UE4SS hands each to ForEach wrapped, and get() unwraps it
local function items(arr)
    local out = {}
    arr:ForEach(function(_, item)
        out[#out + 1] = item:get()
    end)
    return out
end

local function shortNames(arr, field)
    local out = array()
    for _, obj in ipairs(items(arr)) do
        if valid(obj) then
            out[#out + 1] = obj[field]:ToString()
        end
    end
    return out
end

-- Every crowd request this show has scheduled, active or settled; the server picks
local function requests()
    local out = array()
    for _, timing in ipairs(items(first("RequestMgr")._requests)) do
        local entry = {
            id = plain(timing.uniqueRequestId),
            state = plain(timing.State),
            start_measure = plain(timing.RequestStartMeasure),
            bars = plain(timing.AvailabilityDurationInBars),
            procedural = plain(timing.ProceduralRequests),
        }
        local request = timing.MyRequest
        if valid(request) then
            local filter = request.RequestCelFilter
            entry.prompt = text(request.PromptMessage)
            entry.filter = {
                type = plain(filter.FilterTo),
                disc_type = plain(filter.LoopType),
                instrument = plain(filter.instrument),
                genre = plain(filter.Genre),
                years = plain(filter.YearRange),
            }
            local ok, songs = pcall(shortNames, filter.songs, "songShortName")
            entry.filter.songs = ok and songs or NULL
            local ok_c, discs = pcall(shortNames, filter.Cels, "shortName")
            entry.filter.discs = ok_c and discs or NULL
        end
        out[#out + 1] = entry
    end
    return out
end

-- The package a goal's data comes from. The goal on stage holds a runtime copy of its data,
-- ShowGoalQuotaData_2147467126 in the asset's package, while the set's config holds the
-- asset itself, so only the package matches the two
local function goalPackage(data)
    return data:GetFullName():match("^%S+ ([^.]+)")
end

-- The song each goal of the set features, by goalPackage. A goal names it only as
-- {RequiredSong}: the set lists its featured songs, and each section's config of a goal
-- holds an index into that list
local function featuredSongs()
    local prompt = first("ShowFlowMgr")._curPrompt
    local songs = items(prompt.FeaturedSongs)
    local byGoal = {}
    for _, section in ipairs(items(prompt.ShowSections)) do
        for _, config in ipairs(items(section.GoalsThisSection)) do
            local data, song = config.ShowGoalData, songs[config.FeaturedSongIndex + 1]
            if valid(data) and valid(song) and not byGoal[goalPackage(data)] then
                byGoal[goalPackage(data)] = song.songShortName:ToString()
            end
        end
    end
    return byGoal
end

-- Every goal this show has used, each marked whether it is up now. The active ones are a
-- TMap, which UE4SS's Lua reads less surely than an array, so a failure there costs only
-- the mark
local function goals(featured)
    local manager = first("ShowGoalsMgr")
    local active
    pcall(function()
        local found = {}
        manager._currentlyActiveGoals:ForEach(function(_, goal)
            found[goal:get():GetFullName()] = true
        end)
        active = found
    end)
    local out = array()
    for _, goal in ipairs(items(manager._allGoalsUsedThisShow)) do
        if valid(goal) then
            local data = goal._myGoalData
            local entry = {
                kind = goal:GetClass():GetFName():ToString(),
                active = NULL,
                completed = ask(goal, "HasBeenCompleted"),
                progress = ask(goal, "GetProgressTowardsCompletion"),
                performed = ask(goal, "GetNumTimesPerformed"),
                required = ask(goal, "GetRequiredNumActions"),
                bars = ask(goal, "GetDurationInBars"),
                completed_bars = ask(goal, "GetTotalCompletedBars"),
            }
            if active then
                entry.active = active[goal:GetFullName()] == true
            end
            if valid(data) then
                entry.name = text(data.GoalName)
                entry.description = text(data.GoalDescription)
                entry.genre = ask(data, "GetGenre")
                entry.instrument = ask(data, "GetInstrument")
                entry.disc_type = ask(data, "GetLoopType")
                entry.years = ask(data, "GetYearRange")
                if data.UseFeaturedSong then
                    entry.song = featured[goalPackage(data)] or NULL
                end
            end
            out[#out + 1] = entry
        end
    end
    return out
end

-- The campaign layer: crowd requests, goals, the crowd meter and the score. Each part is
-- read on its own, so one that fails leaves the rest, and the failures are listed: in
-- freestyle there are none of them to find.
function ops.set_state()
    local state = { problems = array() }
    local function try(field, read)
        local ok, result = pcall(read)
        if ok then
            state[field] = result
        else
            state[field] = NULL
            state.problems[#state.problems + 1] = field .. ": " .. tostring(result)
        end
    end
    try("measure", function() return first("HypeAudio"):GetCurrentMeasure(RAW) end)
    try("requests", requests)
    -- Apart from the goals, so a set whose songs cannot be read still lists its goals
    local ok, featured = pcall(featuredSongs)
    if not ok then
        state.problems[#state.problems + 1] = "featured songs: " .. tostring(featured)
        featured = {}
    end
    try("goals", function() return goals(featured) end)
    -- How fresh the mix is, which rises on a new drop and fades while nothing changes. It
    -- is not the crowd meter, though it looked like one: it read 0 with the meter halfway
    try("freshness", function()
        local stat = first("SkillStat_Freshness")
        return {
            value = ask(stat, "GetCurStatValue"),
            min = ask(stat, "GetMinValue"),
            max = ask(stat, "GetMaxValue"),
        }
    end)
    -- The crowd meter has no C++ home the dump shows: its widget, a Blueprint, keeps the
    -- health and the rules that move it. Freshness and the VFX manager's CrowdMeterPercent
    -- both looked like it and did not follow the screen
    -- There are five: four are templates in /Game/ packages that FUSER builds the HUD from,
    -- frozen at their defaults, 30 with a warning, and FindFirstOf finds one of those. The
    -- one on screen was made at runtime, so it lives under /Engine/Transient
    try("crowd", function()
        local widget
        for _, w in ipairs(FindAllOf("WBP_ShowsCrowdMeter_C") or {}) do
            local ok, path = pcall(function() return w:GetFullName() end)
            if valid(w) and ok and type(path) == "string" and path:find(" /Engine/Transient", 1, true) then
                widget = w
            end
        end
        if not widget then
            error("no crowd meter has started", 0)
        end
        local out = {}
        for _, field in ipairs({ "CurHealth", "MaxHealth", "CurHealthState", "WarningCount" }) do
            local ok, v = pcall(function() return widget[field] end)
            out[field] = ok and plain(v) or NULL
        end
        return out
    end)
    try("show", function()
        local flow = first("ShowFlowMgr")
        return {
            score = ask(flow, "GetScore"),
            section = ask(flow, "GetCurrentShowSectionIndex"),
            section_progress = ask(flow, "GetCurrentSectionProgress"),
            last_section = ask(flow, "IsInLastSection"),
            has_meter = ask(flow, "DoesCurrentSectionHaveCrowdMeter"),
        }
    end)
    return state
end

-- A value as read can report it: as is, or an object or text by its name
local function readable(v)
    if plain(v) ~= NULL then
        return v
    end
    local named, s = pcall(function() return v:ToString() end)
    if not named or type(s) ~= "string" then
        named, s = pcall(function() return v:GetFullName() end)
    end
    return named and plain(s) or NULL
end

local function readFields(obj, fields)
    local out = {}
    for _, field in ipairs(fields or {}) do
        local ok, v = pcall(function() return obj[field] end)
        local listed, list = false, nil
        if ok and plain(v) == NULL then
            -- A TArray, or a TMap's values, item by item: a featured song is one of these
            listed, list = pcall(items, v)
        end
        if not ok then
            out[field] = NULL
        elseif listed then
            local named = array()
            for i, item in ipairs(list) do
                named[i] = readable(item)
            end
            out[field] = named
        else
            out[field] = readable(v)
        end
    end
    return out
end

-- Fields of an object of a class, read and never written, and nothing called: for finding
-- where FUSER keeps something without a restart for every guess. A field that is an object
-- or text comes back as its name, and an array as a list of them. With all, every object of the class, each with its full
-- name, since the first one FindFirstOf finds can be a stale copy.
function ops.read(args)
    if not args.all then
        return readFields(first(args.class), args.fields)
    end
    local out = array()
    for _, obj in ipairs(FindAllOf(args.class) or {}) do
        if valid(obj) then
            local entry = readFields(obj, args.fields)
            entry["(object)"] = obj:GetFullName()
            out[#out + 1] = entry
        end
    end
    return out
end

-- Several commands in one request, run in order on one frame. A bar's drops have to reach
-- FUSER within a tenth of a beat or so of its downbeat to count as on it, and a round trip
-- each put a bar's fourth drop a third of a beat behind its first.
function ops.batch(args)
    local commands = args.commands or {}
    -- Checked before any runs, so a misspelt command does not leave half a bar played
    for i, command in ipairs(commands) do
        if not ops[command.op] or command.op == "batch" then
            error("command " .. i .. " of the batch, " .. tostring(command.op) .. ", is unknown", 0)
        end
    end
    local results = array()
    for i, command in ipairs(commands) do
        local ok, result = pcall(ops[command.op], command.args or {})
        if not ok then
            error(command.op .. ", command " .. i .. " of the batch: " .. tostring(result), 0)
        end
        results[i] = result
    end
    return results
end

------------------------------------------------------------------------------- ipc

local function ipcDir()
    if CRYSTALIZE_IPC_DIR then
        return CRYSTALIZE_IPC_DIR
    end
    -- .../Mods/Crystalize/Scripts/main.lua -> .../Mods/Crystalize/ipc/
    local source = debug.getinfo(1, "S").source:gsub("^@", "")
    local modDir = source:match("^(.*[/\\])Scripts[/\\][^/\\]+$")
    return (modDir or "Mods/Crystalize/") .. "ipc/"
end

local function respond(dir, id, body)
    body.id = id
    body.session = SESSION
    local name = dir .. "response-" .. tostring(id) .. ".json"
    local text = encode(body) -- before the file, so a failure leaves nothing behind
    local f = assert(io.open(name .. ".tmp", "w"))
    f:write(text)
    f:close()
    -- Renamed into place so the server never reads half a file
    assert(os.rename(name .. ".tmp", name))
end

local function run(request)
    local op = ops[request.op]
    if not op then
        return { ok = false, error = "unknown command " .. tostring(request.op) }
    end
    local ok, result = pcall(op, request.args or {})
    if ok then
        return { ok = true, result = result }
    end
    return { ok = false, error = tostring(result) }
end

local function poll(dir)
    local path = dir .. "request.lua"
    local f = io.open(path, "r")
    if not f then
        return
    end
    local source = f:read("*a")
    f:close()
    os.remove(path)

    -- An empty environment: a request is data, and can call nothing
    local chunk, err = load(source, "request", "t", {})
    local ok, request = false, err
    if chunk then
        ok, request = pcall(chunk)
    end
    if not ok or type(request) ~= "table" then
        log("unreadable request: %s", tostring(request))
        return
    end

    ExecuteInGameThread(function()
        local wrote, werr = pcall(respond, dir, request.id, run(request))
        if not wrote then
            log("could not answer request %s: %s", tostring(request.id), tostring(werr))
        end
    end)
end

local dir = ipcDir()
-- A request written before FUSER started has been given up on already
os.remove(dir .. "request.lua")

LoopAsync(POLL_MS, function()
    local ok, err = pcall(poll, dir)
    if not ok then
        log("%s", tostring(err))
    end
    return false
end)

log("%s loaded, watching %s", VERSION, dir)

return { encode = encode, array = array, NULL = NULL, poll = poll }
