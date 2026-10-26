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
        error(class .. " not found, open a freestyle session first", 0)
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
