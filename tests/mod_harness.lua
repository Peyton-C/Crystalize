-- Loads the mod against a fake FUSER, answers one request, and prints the game calls
-- it made as JSON. Usage: lua mod_harness.lua <main.lua> <ipc dir> [missing class]
--
-- The fake stands in for UE4SS's API: objects answer IsValid, properties are fields, and
-- any method not defined below is recorded instead of run.

local main, dir, missing = arg[1], arg[2], arg[3]
CRYSTALIZE_IPC_DIR = dir

local calls = {}
local mod -- the mod's exports, for its JSON encoder and NULL

local function name(s)
    return { ToString = function() return s end }
end

local INVALID = { IsValid = function() return false end }

local function object(label, fields)
    local o = fields or {}
    o._label = label
    o.IsValid = function() return true end
    return setmetatable(o, {
        __index = function(_, method)
            return function(_, ...)
                local args = mod.array()
                for i = 1, select("#", ...) do
                    local v = select(i, ...)
                    if v == nil then
                        v = mod.NULL
                    elseif type(v) == "table" and v._label then
                        v = v._label
                    end
                    args[i] = v
                end
                calls[#calls + 1] = { object = label, method = method, args = args }
            end
        end,
    })
end

local song = object("CallMeMaybe", {
    songShortName = name("callmemaybe"), artist = name("Carly Rae Jepsen"), Genre = 6, year = 2012,
    IsOwnedAndInstalled = function() return true end,
})
local dlc = object("KingsAndQueens", {
    songShortName = name("kingsandqueens"), artist = name("Thirty Seconds to Mars"), Genre = 8,
    year = 2009, IsOwnedAndInstalled = function() return false end,
})
local function tarray(n)
    return { GetArrayNum = function() return n end }
end

local function cel(short, celType, of)
    return object(short, {
        shortName = name(short), Title = name("Call Me Maybe"), CelType = celType,
        Key = 7, mode = 0, BPM = 120, instrument = 3, song = of or song, SongLengthTicks = 30720,
        Transposes = { C = -5, Db = -6, D = 5, Eb = 4, E = 3, F = 2, Gb = 1, G = 0, Ab = -1,
                       A = -2, Bb = -3, B = -4 },
        MajorMidiSongAssets = tarray(1), MinorMidiSongAssets = tarray(0),
    })
end
local cels = { cel("callmemaybebt", 0), cel("callmemaybebs", 1), cel("kingsandqueensbt", 0, dlc) }

local function disc(short)
    return object("disc " .. short, { GetCelShortName = function() return name(short) end })
end

-- A TArray as UE4SS's Lua hands one over: ForEach passes each item wrapped, get() unwraps.
-- A TMap's ForEach passes its keys and values the same way
local function tarrayOf(list)
    return { ForEach = function(_, fn)
        for i, v in ipairs(list) do
            fn(i, { get = function() return v end })
        end
    end }
end

local function goal(label, goalName, done)
    return object(label, {
        _myGoalData = object(label .. " data", { GoalName = name(goalName), GoalDescription = name("") }),
        GetFullName = function() return label end,
        GetClass = function() return { GetFName = function() return name("ShowGoalQuota") end } end,
        HasBeenCompleted = function() return done end,
        GetNumTimesPerformed = function() return done and 1 or 0 end,
        GetRequiredNumActions = function() return 1 end,
    })
end
local doneGoal = goal("GoalBeat", "Drop a {btType}", true)
local openGoal = goal("GoalBass", "Drop a {bsType}", false)

local synthRequest = object("SynthRequest", {
    PromptMessage = name("Some {Instrument} will make my day!"),
    RequestCelFilter = { FilterTo = 1, LoopType = 0, instrument = 5, Genre = 0, YearRange = 0,
                         songs = tarrayOf({}), Cels = tarrayOf({}) },
})

local world = {
    BP_Deck_C = {
        object("Deck0", { track = 0, CurPlayingDisc = disc("callmemaybebt"), CurQueuedDisc = INVALID }),
        object("Deck1", { track = 1, CurPlayingDisc = INVALID, CurQueuedDisc = disc("callmemaybebs") }),
        object("Deck2", { track = 2, CurPlayingDisc = INVALID, CurQueuedDisc = INVALID }),
        object("Deck3", { track = 3, CurPlayingDisc = INVALID, CurQueuedDisc = INVALID }),
    },
    SongCelData = cels,
    HypeAudio = { object("HypeAudio", {
        GetKey = function() return 7 end, GetKeyMode = function() return 1 end,
        GetCurrentBPM = function() return 120 end,
        GetMinBpm = function() return 60 end, GetMaxBpm = function() return 200 end,
        -- Recorded like an undefined method, so test_clock still sees it asked raw
        GetCurrentMeasure = function(_, timeType)
            calls[#calls + 1] = { object = "HypeAudio", method = "GetCurrentMeasure", args = mod.array({ timeType }) }
            return 84
        end,
    }) },
    RequestMgr = { object("RequestMgr", { _requests = tarrayOf({
        { uniqueRequestId = -1, State = 1, RequestStartMeasure = 82, AvailabilityDurationInBars = 14,
          ProceduralRequests = 1, MyRequest = synthRequest },
        { uniqueRequestId = -1, State = 0, RequestStartMeasure = 110, AvailabilityDurationInBars = 12,
          ProceduralRequests = 1, MyRequest = INVALID },
    }) }) },
    ShowGoalsMgr = { object("ShowGoalsMgr", {
        _allGoalsUsedThisShow = tarrayOf({ doneGoal, openGoal }),
        _currentlyActiveGoals = tarrayOf({ openGoal }),
    }) },
    SkillStat_Freshness = { object("Freshness", {
        GetCurStatValue = function() return 0.5 end,
        GetMinValue = function() return 0 end, GetMaxValue = function() return 1 end,
    }) },
    ShowFlowMgr = { object("ShowFlowMgr", { GetScore = function() return 1500 end }) },
    BP_Crowd_Manager_C = { object("Crowd", { CurrentCrowdMeterState = 1 }) },
    -- As in FUSER: a template comes first, frozen at its defaults; the one on screen was
    -- made at runtime
    WBP_ShowsCrowdMeter_C = {
        object("StaleMeter", { CurHealth = 30, MaxHealth = 100, CurHealthState = 1, WarningCount = 1,
            GetFullName = function()
                return "WBP_ShowsCrowdMeter_C /Game/Gameplay/Hud/WBP_Hud.WidgetArchetype:WidgetTree_0.CrowdMeter"
            end }),
        object("LiveMeter", { CurHealth = 62, MaxHealth = 100, CurHealthState = 2, WarningCount = 0,
            GetFullName = function()
                return "WBP_ShowsCrowdMeter_C /Engine/Transient.GameEngine_1:WBP_Hud.WidgetTree_0.CrowdMeter"
            end }),
    },
    DeskState = { object("DeskState") },
    AudioController = { object("AudioController") },
}
if missing then
    world[missing] = nil
end

function FindAllOf(class)
    return world[class]
end

function FindFirstOf(class)
    return (world[class] or {})[1] or INVALID
end

function ExecuteInGameThread(fn)
    fn()
end

function LoopAsync() end

print = function() end -- the mod's own log lines would muddle the JSON on stdout

-- The mod deletes a request left from before it loaded, so this one goes back after
local f = assert(io.open(dir .. "request.lua"))
local request = f:read("a")
f:close()
mod = dofile(main)
f = assert(io.open(dir .. "request.lua", "w"))
f:write(request)
f:close()

mod.poll(dir)
io.stdout:write(mod.encode(mod.array(calls)))
