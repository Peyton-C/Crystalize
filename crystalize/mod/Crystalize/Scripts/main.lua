-- Crystalize: runs the Crystalize MCP server's commands inside FUSER.
--
-- The server writes ipc/request.lua, a Lua table literal, and this mod answers with
-- ipc/response-<id>.json. UE4SS's Lua has no sockets and FUSER runs under Wine, so a
-- shared folder is the channel; a table literal means load() replaces a JSON parser.
--
-- All of it runs from LoopInGameThreadWithDelay, which rides UE4SS's EngineTick hook.
-- HookUObjectProcessEvent must stay off: with it on, a Blueprint event during the
-- startup logos returns garbage and FUSER crashes on it.
--
-- LoopAsync and ExecuteInGameThread must not be used to get there. ExecuteInGameThread
-- pushes onto and pops from the Lua stack game-thread actions run on, and when called
-- from LoopAsync's thread it does that with nothing keeping the game thread out: UE4SS
-- drops its mutex to run an action, and Lua's lock is released during C functions. A C
-- function then reads the wrong stack slot as a table, which pcall cannot catch.

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

-- How FUSER starts the name of a disc recorded on an instrument
local CRAFTED = "CraftedInstrument_"

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
    -- A recorded disc is an instrument's cel, made when it was spawned, and drops like any
    for _, c in ipairs(FindAllOf("InstrumentCelData") or {}) do
        if valid(c) and c.shortName:ToString():find(CRAFTED, 1, true) == 1 then
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

local ops = {}

function ops.ping()
    return { version = VERSION }
end

-- A deck's volume from 0 to 1, or NULL where it cannot be read
local function deckGain(track)
    local ok, gain = pcall(function() return FindFirstOf("DeckState"):GetDeckGain(track) end)
    return ok and plain(gain) or NULL
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
            gain = deckGain(track),
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
        -- Where every disc's loop starts, in beats: a disc is that far past it, wrapped at
        -- its length, which places its pickups on the clock
        sync_beat = ask(audio, "GetDefaultSyncBeat"),
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
            -- The beats in its loop where a pickup drop can land, which some older custom
            -- songs have none of
            local ok_p, pickups = pcall(function()
                local beats = array()
                for i, beat in ipairs(items(cel.PickupBeats)) do
                    beats[i] = plain(beat)
                end
                return beats
            end)
            disc.pickups = ok_p and pickups or NULL
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

-- What the hot swap key does: the disc cued on a deck replaces the one playing there.
-- Swap goals count this, and not the same change made by a drop or drop_queue
function ops.hot_swap(args)
    first("DeskState"):HotSwapOnDeck(checkDeck(args.deck))
    return { deck = args.deck }
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

-- One step of a fade. FUSER has no fade of its own, the deck's fader only moves in steps
function ops.gain(args)
    first("AudioController"):NetSetGainForTrack(checkDeck(args.deck), args.gain)
    return { deck = args.deck, gain = args.gain }
end

-- What the riser key does. The widget chooses the riser, and TransitionMgr.PlayTransition
-- would need a soft pointer UE4SS cannot make. The mode stays set, as the menu's would, and
-- only the mode changes: UE4SS takes the options as a table but wants every member
function ops.riser(args)
    local mgr = first("TransitionMgr")
    if mgr:IsInTransition() then
        error("a riser is already playing", 0)
    end
    local options = mgr:GetTransitionOptions()
    mgr:SetTransitionOptions({
        TempoKeyModeBehavior = args.mode,
        DropBehavior = options.DropBehavior,
        ManualBPM = options.ManualBPM,
        ManualKey = options.ManualKey,
        ManualMode = options.ManualMode,
        SyncsToMasterTimeline = options.SyncsToMasterTimeline,
        PlaySongTransitionCels = options.PlaySongTransitionCels,
    })
    first("WBP_TransitionTrigger_C"):TriggerTransition({})
    if not mgr:IsInTransition() then
        error("FUSER did not start the riser", 0)
    end
    return { mode = args.mode, start = mgr:GetTransitionStartBeat(), drop = mgr:GetTransitionDropBeat() }
end

function ops.solo(args)
    first("AudioController"):NetSoloTrack(checkDeck(args.deck), args.on == true)
    return { deck = args.deck, on = args.on == true }
end

-- What the Take Snapshot key does: saves the mix playing as a moment in the player's
-- profile, which the campaign's snapshot goals count
function ops.snapshot()
    first("SavedMomentsMgr"):SaveCurrentMoment()
    return {}
end

------------------------------------------------------------------------------- instruments

-- An instrument is played as its panel plays it, without the panel: InstrumentCraftingState
-- loads one into a crafting slot, 0 to 3 and not a deck, and takes each pad press. Each bar
-- a pad is held through counts once to a Perform goal, a tap and a held press alike.

-- EGridInstrumentMode: Soloing plays over the mix, as a performance, where Crafting records
local CRAFTING, SOLOING = 0, 1

local function instrumentEntry(inst)
    return { name = inst.shortName:ToString(), title = text(inst.Title) }
end

-- Every instrument FUSER has loaded, MIDI canvases and any other kind. A placeholder
-- named None is among them, and so is every disc recorded so far, which is not an instrument
function ops.instruments()
    local out = array()
    for _, inst in ipairs(FindAllOf("InstrumentCelData") or {}) do
        if valid(inst) then
            local ok, entry = pcall(instrumentEntry, inst)
            if ok and entry.name ~= "None" and entry.name:find(CRAFTED, 1, true) ~= 1 then
                out[#out + 1] = entry
            end
        end
    end
    return out
end

local function findInstrument(name)
    for _, inst in ipairs(FindAllOf("InstrumentCelData") or {}) do
        if valid(inst) and inst.shortName:ToString() == name then
            return inst
        end
    end
    error("no instrument named " .. tostring(name), 0)
end

local function checkSlot(slot)
    if math.type(slot) ~= "integer" or slot < 0 or slot > 3 then
        error("slot must be 0 to 3, got " .. tostring(slot), 0)
    end
    return slot
end

-- Whether a slot's instrument has loaded and can take a press
local function slotState(slot)
    local mgr = first("InstrumentCraftingMgr")
    local crafter = mgr:GetCrafterInSlot(slot)
    return {
        slot = slot,
        crafting = mgr:IsCrafting(slot) == true,
        ready = valid(crafter) and ask(crafter, "IsFullyInitialized") == true,
    }
end

function ops.perform_begin(args)
    local slot = checkSlot(args.slot)
    first("InstrumentCraftingState"):NetBeginCrafting(slot, findInstrument(args.name))
    return slotState(slot)
end

function ops.perform_state(args)
    return slotState(checkSlot(args.slot))
end

-- A pad pressed or let go. UE4SS takes the FGridInstrumentCraftingState as a table
function ops.perform_press(args)
    local slot = checkSlot(args.slot)
    first("InstrumentCraftingState"):NetSetGridCraftingState(slot, {
        PrimaryActionOn = args.on == true, SecondaryActionOn = false,
        GridPos = { X = args.x or 0, Y = args.y or 0 }, mode = args.record and CRAFTING or SOLOING,
    })
    return { slot = slot, on = args.on == true }
end

-- Turns what the slot has recorded into a disc, on the deck FUSER keeps for its type. It has
-- no way to name another deck, so a disc wanted elsewhere is dropped there after, by name
function ops.record_spawn(args)
    local slot = checkSlot(args.slot)
    first("InstrumentCraftingState"):NetSpawnCraftedDisc(slot, false, args.queue == true)
    return { slot = slot, queued = args.queue == true }
end

function ops.perform_end(args)
    local slot = checkSlot(args.slot)
    first("InstrumentCraftingState"):NetEndCrafting(slot)
    return { slot = slot }
end

------------------------------------------------------------------------------- effects

-- An effect is switched as the deck's FX panel switches it, without the panel:
-- AudioEffectState holds a slot for each deck and one for the master, each loaded with an
-- effect and turned on and off. A Use FX goal counts the effect turned on over a deck
-- holding the disc it wants. Slots 0 to 2 are FUSER's own, and its tricks ignore them.

-- EAudioEffectSlot: Deck0Slot, then a slot a deck, then MasterSlot, which covers all four
local DECK0_SLOT, MASTER_SLOT = 3, 7

local function effectEntry(fx)
    return { name = fx.EffectShortName:ToString(), title = text(fx.EffectName) }
end

-- Every effect FUSER has, the set's own or not: like discs, any of them counts. The
-- transition effect, which no deck can hold, hides from the UI and from this
function ops.effects()
    local out = array()
    for _, fx in ipairs(FindAllOf("AudioEffectData") or {}) do
        if valid(fx) and not fx.HideFromUI then
            out[#out + 1] = effectEntry(fx)
        end
    end
    return out
end

local function findEffect(name)
    for _, fx in ipairs(FindAllOf("AudioEffectData") or {}) do
        if valid(fx) and fx.EffectShortName:ToString() == name then
            return fx
        end
    end
    error("no effect named " .. tostring(name), 0)
end

-- A deck's slot, or the master's for no deck
local function effectSlot(deck)
    if deck == nil or deck == NULL then
        return MASTER_SLOT
    end
    return DECK0_SLOT + checkDeck(deck)
end

-- The effect a slot holds, or nil. Read from the slot's FAudioEffectInfo, since
-- TryGetEffectData hands back nothing UE4SS can read
local function slotEffect(state, slot)
    local found
    state._slotEffectInfo:ForEach(function(i, info)
        if i == slot + 1 then
            local fx = info:get().effectData
            found = valid(fx) and fx or nil
        end
    end)
    return found
end

-- Each deck's effect and the master's, in that order, or null for an empty slot
function ops.fx_state()
    local state = first("AudioEffectState")
    local out = array()
    for slot = DECK0_SLOT, MASTER_SLOT do
        local fx = slotEffect(state, slot)
        if fx then
            local entry = effectEntry(fx)
            entry.on = state:GetIsEnabled(slot) == true
            out[#out + 1] = entry
        else
            out[#out + 1] = NULL
        end
    end
    return out
end

-- Loads an effect into a deck's slot, or the master's, if it holds another, and turns it
-- on or off. Without a name, the slot keeps the one it has. FUSER grades the switch by when
-- it arrives, as it does a drop. Code that waits for the downbeat on the game thread gets
-- Bad every time, since the game judges it by a clock that stood still while it waited
function ops.fx(args)
    local state = first("AudioEffectState")
    local slot = effectSlot(args.deck)
    local fx = slotEffect(state, slot)
    -- By name, since UE4SS can hand the same object back as two userdata that differ
    if args.name ~= nil and args.name ~= NULL
            and not (fx and fx.EffectShortName:ToString() == args.name) then
        fx = findEffect(args.name)
        state:NetSetAddEffectToSlot(slot, fx)
    end
    if not fx then
        error("that slot holds no effect; name one", 0)
    end
    state:NetSetEffectEnabled(slot, args.on == true)
    local entry = effectEntry(fx)
    entry.on = args.on == true
    return entry
end

------------------------------------------------------------------------------- launching

-- What FUSER's menus do between the title screen and a set, done without them. Each op is
-- one step; the order, and the waiting between them while a map loads, is launch.py's.

-- EShellScreenType
local SCREEN_MAIN_MENU, SCREEN_GAMEPLAY, SCREEN_SHOW_BREAK = 3, 10, 48
-- EHypeGameMode
local MODE_SHOW = 1

local function venueAt(index)
    local venue = items(first("VenueMgr").Venues)[index + 1]
    if not valid(venue) then
        error("no venue " .. tostring(index), 0)
    end
    return venue
end

-- Each prompt is checked before it is touched: UE4SS reads through a null object without
-- a check, FUSER crashes past pcall, and a listing of every venue's sets did just that
local function promptAt(venue, index)
    local prompt = items(venue.MissionPrompts)[index + 1]
    if not valid(prompt) then
        error("no set " .. tostring(index) .. " in that venue", 0)
    end
    return prompt
end

-- The session's flow manager. The menu's backdrop map has a freestyle one of its own, so
-- the class comes from the mode the last load was configured for
local function flowMgr(mode)
    local f = FindFirstOf(mode == MODE_SHOW and "ShowFlowMgr" or "FreestyleFlowMgr")
    return valid(f) and f or nil
end

-- Where FUSER is: the screen (EShellScreenType), the title screen's sign-in
-- (ETitleScreenState), the mode a load was configured for (EHypeGameMode), and the
-- session's flow (EGameFlowState) with the manager's full name, which changes when a map
-- loads. Read while a map loads, so each part is null if it cannot be read yet.
function ops.flow()
    local out = { screen = NULL, title = NULL, mode = NULL, flow = NULL, flow_object = NULL }
    local shell = FindFirstOf("ShellFlowMgr")
    if valid(shell) then
        out.screen = shell:GetCurrentScreen()
    end
    local title = FindFirstOf("HypeTitleScreen")
    if valid(title) then
        out.title = title:GetTitleScreenState()
    end
    local cfg = FindFirstOf("LevelLoadConfig")
    if valid(cfg) then
        out.mode = cfg._gameMode
    end
    local f = flowMgr(out.mode)
    if f then
        out.flow = f.CurrentState
        out.flow_object = f:GetFullName()
    end
    return out
end

-- The sets of each venue asked for, by index from 0, in the order the game lists them
function ops.sets(args)
    local out = array()
    for i, index in ipairs(args.venues or {}) do
        local venue = venueAt(index)
        local sets = array()
        for j, prompt in ipairs(items(venue.MissionPrompts)) do
            sets[j] = valid(prompt) and {
                id = prompt:GetFullName():match("([^.]+)$"),
                name = text(prompt.MissionName),
            } or NULL
        end
        out[i] = sets
    end
    return out
end

-- What a key press on the title screen does: signs in and loads the save. The intro
-- cinematic can still be playing
function ops.sign_in()
    first("HypeTitleScreen"):StartLoginFlow()
    return {}
end

-- Loads a campaign set's venue, configured for that set, with the discs the set gives,
-- which the skipped loadout screen would have added. Unlocked or not: nothing checks
function ops.load_set(args)
    local venue = venueAt(args.venue)
    local prompt = promptAt(venue, args.set)
    local cfg, mgr = first("LevelLoadConfig"), first("VenueMgr")
    cfg:ConfigureShow(prompt)
    first("CrateMgr"):ApplyCrateFilter(prompt)
    mgr:SetCurrentVenueData(venue, false)
    mgr:LoadCurrentVenueMap(cfg, true)
    return { venue = venue:GetFullName(), set = prompt:GetFullName() }
end

-- Loads freestyle, in a venue by index or the last one played
function ops.load_freestyle(args)
    local cfg, mgr = first("LevelLoadConfig"), first("VenueMgr")
    local venue = args.venue and venueAt(args.venue) or mgr:GetLastFreestyleVenueData()
    if not valid(venue) then
        error("no freestyle venue played yet, give one", 0)
    end
    cfg:ConfigureFreestyle()
    mgr:SetCurrentVenueData(venue, true)
    mgr:LoadCurrentVenueMap(cfg, true)
    return { venue = venue:GetFullName() }
end

-- Starts a loaded session: the gameplay screen, which the map load leaves unset, then
-- each flow state in turn. A show stops in Intro without the second
function ops.begin(args)
    local cfg = first("LevelLoadConfig")
    local f = flowMgr(cfg._gameMode)
    if not f then
        error("nothing loaded to start", 0)
    end
    first("ShellFlowMgr"):LoadShellScreen(SCREEN_GAMEPLAY, false, false)
    for _, state in ipairs(args.states or {}) do
        f:GoToState(state, {}, false)
    end
    return { flow = f.CurrentState }
end

-- Back to the main menu, reloading its map. A set started this way ends on a rewards
-- screen that waits for a desk outro the skipped steps never play, and this leaves it
function ops.main_menu()
    first("ShellFlowMgr"):GoBackToScreen(SCREEN_MAIN_MENU, true)
    return {}
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

-- An FCelFilter, which says what discs a crowd request or a goal wants. Its FilterTo says
-- which of the other fields counts
local function celFilter(filter)
    local out = {
        type = plain(filter.FilterTo),
        disc_type = plain(filter.LoopType),
        instrument = plain(filter.instrument),
        genre = plain(filter.Genre),
        years = plain(filter.YearRange),
    }
    local ok, songs = pcall(shortNames, filter.songs, "songShortName")
    out.songs = ok and songs or NULL
    local ok_c, discs = pcall(shortNames, filter.Cels, "shortName")
    out.discs = ok_c and discs or NULL
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
            entry.prompt = text(request.PromptMessage)
            entry.filter = celFilter(request.RequestCelFilter)
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
-- holds an index into that list. One goal can come back in a later section featuring
-- another song, set 5's pickup goal first Push It and then Never Gonna Give You Up, so
-- the song is the one of the section playing, or the last before it that had the goal
local function featuredSongs()
    local flow = first("ShowFlowMgr")
    local prompt = flow._curPrompt
    local current = ask(flow, "GetCurrentShowSectionIndex")
    local songs = items(prompt.FeaturedSongs)
    local byGoal = {}
    for i, section in ipairs(items(prompt.ShowSections)) do
        local index = i - 1  -- FUSER counts sections from 0
        if current == NULL or index <= current then
            for _, config in ipairs(items(section.GoalsThisSection)) do
                local data, song = config.ShowGoalData, songs[config.FeaturedSongIndex + 1]
                if valid(data) and valid(song) then
                    byGoal[goalPackage(data)] = song.songShortName:ToString()
                end
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
                -- What the goal counts, as a request's filter says it. Read from the goal's
                -- own copy of its data, where FUSER can fill in what it picked. A quota goal
                -- ("Drop a ...") keeps its filters in CelFilters; a stat goal ("Include 2+
                -- {Genre} discs") one level down, in each of its MixFilterSet's mix filters
                local ok, filters = pcall(function()
                    local found = array()
                    local function add(set)
                        for _, filter in ipairs(items(set.Filters)) do
                            found[#found + 1] = celFilter(filter)
                        end
                    end
                    pcall(add, data.CelFilters)
                    pcall(function()
                        for _, mix in ipairs(items(data.MixFilterSet.MixFilters)) do
                            add(mix.FilterSet)
                        end
                    end)
                    return found
                end)
                entry.filters = ok and filters or NULL
                if data.UseFeaturedSong then
                    entry.song = featured[goalPackage(data)] or NULL
                end
                -- The instrument a Perform goal names as {CustomInstrument}, by its title
                if data.UseFeaturedInstrument then
                    local ok_i, inst = pcall(function() return data:GetCustomInstrument() end)
                    entry.custom_instrument = ok_i and text(inst) or NULL
                end
                -- The effect a Use FX goal names as {Effect}. The goal's copy of its data
                -- holds the effect itself, where GetAudioEffect gives only its title
                if data.UseFeaturedEffect then
                    local ok_e, fx = pcall(function() return effectEntry(data.RequiredAudioEffect) end)
                    entry.effect = ok_e and fx or NULL
                end
                -- The tempo a Stay in a tempo goal names as {AbsTempo}. Only stat goals
                -- have the field, and a goal not about tempo holds 0
                local ok_t, tempo = pcall(function() return data.TempoRequirement end)
                if ok_t and math.type(tempo) == "integer" and tempo > 0 then
                    entry.tempo = tempo
                end
                -- The key and the mode a key goal names as {MusicalKey} and {MusicalMode},
                -- as FUSER writes them, "C" and "Minor". Its fields hold enums whose none
                -- differs between goals, and these give empty text for a goal not about key
                for field, method in pairs({ key = "GetKey", mode = "GetMode" }) do
                    local ok_k, words = pcall(function() return text(data[method](data)) end)
                    if ok_k and type(words) == "string" and words ~= "" then
                        entry[field] = words
                    end
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
    -- The instruments the set features, which its Perform goals name
    try("instruments", function()
        local out = array()
        for _, inst in ipairs(items(first("ShowFlowMgr")._curPrompt.FeaturedInstruments)) do
            if valid(inst) then
                out[#out + 1] = instrumentEntry(inst)
            end
        end
        return out
    end)
    -- The effects the set features, which its Use FX goals name
    try("effects", function()
        local out = array()
        for _, fx in ipairs(items(first("ShowFlowMgr")._curPrompt.FeaturedEffects)) do
            if valid(fx) then
                out[#out + 1] = effectEntry(fx)
            end
        end
        return out
    end)
    -- How fresh the mix is, which rises on a new drop and fades while nothing changes. It
    -- is not the crowd meter, though it looked like one: it read 0 with the meter halfway
    try("freshness", function()
        local stat = first("SkillStat_Freshness")
        local out = {
            value = ask(stat, "GetCurStatValue"),
            min = ask(stat, "GetMinValue"),
            max = ask(stat, "GetMaxValue"),
            -- Freshness has a target zone the set pays for staying in, by the interval,
            -- and scales its bonuses down above it: higher is not simply better
            in_zone = ask(stat, "IsPassingConditions"),
            zone_points = ask(stat, "GetPointsEarnedThisInterval"),
            unique_mix = ask(stat, "IsCurrentMixUnique"),
            zone = NULL,
        }
        -- A struct, which read cannot show, so its two ends are read here
        local ok, zone = pcall(function()
            local range = stat._freshnessStatData.TargetZoneRange
            return array({ plain(range.X), plain(range.Y) })
        end)
        if ok then
            out.zone = zone
        end
        return out
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
        -- Past the health, what could stop actions counting: the crowd's overactivity
        -- rule, and a drain or failure the meter has paused for
        for _, field in ipairs({ "CurHealth", "MaxHealth", "CurHealthState", "WarningCount",
                                 "PrevWarningCount", "OveractivityEnabled", "OveractiveThreshold",
                                 "DrainPaused", "FailurePending" }) do
            local ok, v = pcall(function() return widget[field] end)
            if ok and type(v) == "boolean" then
                out[field] = v
            else
                out[field] = ok and plain(v) or NULL
            end
        end
        -- The actions inside its overactivity window, as it keeps them
        local ok, n = pcall(function() return #items(widget.ActionTimes) end)
        out.ActionCount = ok and n or NULL
        return out
    end)
    try("show", function()
        local flow = first("ShowFlowMgr")
        local show = {
            score = ask(flow, "GetScore"),
            section = ask(flow, "GetCurrentShowSectionIndex"),
            section_progress = ask(flow, "GetCurrentSectionProgress"),
            last_section = ask(flow, "IsInLastSection"),
            has_meter = ask(flow, "DoesCurrentSectionHaveCrowdMeter"),
            -- The show's objects stay on the results screen, frozen, so a set that ended
            -- still reads; FUSER counting it out is what says it is over
            ending = ask(flow, "HasShowEndCountdownBeenBroadcast"),
            -- Whether the show is running or paused between sections, which may be when
            -- actions stop counting
            started = ask(flow, "DidShowStart"),
            looping = ask(flow, "IsCurrentSectionLooping"),
            section_warning = ask(flow, "HasSectionWarningBeenBroadcast"),
            replaying_break = ask(flow, "IsReplayingShowBreakMessage"),
            has_goals = ask(flow, "DoesCurrentSectionHaveShowGoals"),
            -- The points a section ends on for each band of the meter, which say what band
            -- the crowd-meter points came from
            meter_bonus = {
                low = ask(flow, "GetCrowdMeterLowHealthBonus"),
                medium = ask(flow, "GetCrowdMeterMediumHealthBonus"),
                high = ask(flow, "GetCrowdMeterHighHealthBonus"),
                peak = ask(flow, "GetCrowdMeterPeakHealthBonus"),
            },
        }
        -- FShowScoreResults: the stars so far, and the points split by where they came
        -- from, timing grades included
        local ok, results = pcall(function()
            local r = flow:GetScoringResults()
            local points = r.PointsEarned
            local out = {
                stars = plain(r.StarsEarned), failed = plain(r.FailedShow),
                requests = plain(r.NumRequestsSatisfied),
                goal_actions = plain(r.NumGoalActionsSatisfied),
                goals_percent = plain(r.PercentageOfGoalsCompleted),
                requests_percent = plain(r.PercentageOfRequestsCompleted),
                points = {},
            }
            for _, field in ipairs({ "RequestPoints", "RequestComboBonusPoints", "ShowGoalPoints",
                                     "BadTimingBonusPoints", "EarlyTimingBonusPoints",
                                     "LateTimingBonusPoints", "PerfectTimingBonusPoints",
                                     "CrowdMeterPoints" }) do
                out.points[field] = plain(points[field])
            end
            return out
        end)
        show.results = ok and results or NULL
        return show
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

-- A value as a response can carry it: a TMap as pairs, a TArray as a list, a Lua table
-- field by field, and anything else as readable gives it
local function describe(v)
    if plain(v) ~= NULL or v == nil or v == NULL then
        return plain(v)
    end
    -- A TMap, whose ForEach hands the key wrapped as it does the value, as pairs, so a map
    -- keyed by an enum says which entry is which
    local mapped, map = pcall(function()
        local pairs_ = array()
        v:ForEach(function(key, value)
            pairs_[#pairs_ + 1] = array({ readable(key:get()), readable(value:get()) })
        end)
        return pairs_
    end)
    if mapped then
        return map
    end
    -- A TArray, item by item: a featured song is one of these
    local listed, list = pcall(items, v)
    if listed then
        local named = array()
        for i, item in ipairs(list) do
            named[i] = readable(item)
        end
        return named
    end
    -- A Lua table the debug op's code made, not a game object, which has IsValid
    if type(v) == "table" and v.IsValid == nil then
        local out = #v > 0 and array() or {}
        for k, item in pairs(v) do
            out[k] = describe(item)
        end
        return out
    end
    return readable(v)
end

local function readFields(obj, fields)
    local out = {}
    for _, field in ipairs(fields or {}) do
        local ok, v = pcall(function() return obj[field] end)
        out[field] = ok and describe(v) or NULL
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

-- Lua run on the game thread, with UE4SS's globals and the mod's helpers, for trying a
-- FUSER function before it gets a command of its own: a change to the mod costs a FUSER
-- restart, and this costs none. The server offers it only when started with --debug.
-- What the code returns comes back as describe gives it, with anything it printed.
function ops.lua(args)
    local printed = array()
    local env = setmetatable({
        valid = valid, first = first, items = items, readable = readable, describe = describe,
        print = function(...)
            local parts = {}
            for i = 1, select("#", ...) do
                parts[i] = tostring((select(i, ...)))
            end
            printed[#printed + 1] = table.concat(parts, "\t")
        end,
    }, { __index = _G })
    local chunk, err = load(args.code or "", "lua", "t", env)
    if not chunk then
        error(err, 0)
    end
    return { result = describe(chunk()), printed = printed }
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

------------------------------------------------------------------------------- show breaks

-- EGameFlowState
local SHOW_BREAK = 5
local BREAK_CHECK_MS = 500
-- How many checks the dialog stays on a page before it is closed
local SLIDE_CHECKS = 2
-- How many checks a break with messages may go without showing a page
local STUCK_CHECKS = 40

-- A show break's dialog ("Hold Continue") holds FUSER between sections, and no goal or
-- trick counts until the break ends, so the mod closes it on its own rather than wait
-- for a request: a break can open while the agent is between tool calls. The decision is
-- the mod's because nothing on the server's side runs then.
-- _HandleShowBreakProgression ends the whole break once the dialog is on a page. Called
-- earlier, during the camera move a set's opening break starts with, it hid the dialog
-- and the break never ended; _GoToNextBreakMessage then ended it, so that is the way out
-- of a break that never shows a page. On a page, _GoToNextBreakMessage and the
-- WBP_ShowBreak_C widget's AdvanceShowFlow left the dialog up.
-- A break with no messages is left alone: it ends by itself, after 2 to 10 bars.
-- Only the two fields are read until the show is in a break on the gameplay screen or
-- the show-break screen FUSER switches to for the dialog: calling the flow manager's
-- functions while go_to replaced the map crashed FUSER.
local slideChecks, stuckChecks, closed = 0, 0, false

local function closeBreak()
    local cfg = FindFirstOf("LevelLoadConfig")
    local shell = FindFirstOf("ShellFlowMgr")
    local f = valid(cfg) and valid(shell) and flowMgr(cfg._gameMode)
    local screen = f and cfg._gameMode == MODE_SHOW and f.CurrentState == SHOW_BREAK
        and shell:GetCurrentScreen()
    if (screen ~= SCREEN_GAMEPLAY and screen ~= SCREEN_SHOW_BREAK)
            or f:GetNumMessagesThisBreak() == 0 then
        slideChecks, stuckChecks, closed = 0, 0, false
        return
    end
    if closed then
        return
    end
    if f:IsOnFirstSlideOfMessages() or f:IsOnLastSlideOfMessages() then
        slideChecks = slideChecks + 1
        if slideChecks >= SLIDE_CHECKS then
            f:_HandleShowBreakProgression()
            closed = true
            log("closed a show break")
        end
        return
    end
    stuckChecks = stuckChecks + 1
    if stuckChecks >= STUCK_CHECKS then
        f:_GoToNextBreakMessage()
        closed = true
        log("ended a show break that showed no page")
    end
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

    local wrote, werr = pcall(respond, dir, request.id, run(request))
    if not wrote then
        log("could not answer request %s: %s", tostring(request.id), tostring(werr))
    end
end

local dir = ipcDir()
-- A request written before FUSER started has been given up on already
os.remove(dir .. "request.lua")

LoopInGameThreadWithDelay(POLL_MS, function()
    local ok, err = pcall(poll, dir)
    if not ok then
        log("%s", tostring(err))
    end
end)

LoopInGameThreadWithDelay(BREAK_CHECK_MS, function()
    local ok, err = pcall(closeBreak)
    if not ok then
        log("show break: %s", tostring(err))
    end
end)

log("%s loaded, watching %s", VERSION, dir)

return { encode = encode, array = array, NULL = NULL, poll = poll, closeBreak = closeBreak }
