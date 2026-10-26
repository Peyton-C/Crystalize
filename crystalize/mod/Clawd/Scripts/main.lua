-- Clawd: replaces FUSER's avatars with Clawd in headphones.
--
-- The avatar stays, hidden, so its animation still runs, and Clawd takes over one of its
-- own static mesh components, attached to a spine bone so he rides the dance. A separate
-- actor showed in the menus but never on stage: the stage view draws the avatar's own
-- components and nothing else, so Clawd has to be one of them.
--
-- The mesh comes from Clawd_P.pak. It was cooked for Linux, the only target this engine
-- build has, so it carries no materials: each colour slot gets a copy of the engine's
-- BasicShapeMaterial here.
--
-- Everything runs from a one-second poll. UE4SS's ProcessEvent hook crashes FUSER, so
-- there is no BeginPlay to hook, and the poll also puts Clawd back after the game
-- reapplies the outfit, which resets the part he lives in.

local MESH = "/Game/Mods/Clawd/clawd.clawd"
local BASE_MATERIAL = "/Engine/BasicShapes/BasicShapeMaterial.BasicShapeMaterial"

-- Tuned by eye in the game: Clawd's size, how far above the floor his feet are, and his yaw
local SCALE = 2
local LIFT = 30
-- FUSER's characters face 90 degrees off their actor's forward, so 0 and 180 both
-- showed Clawd's side; 270 was found with F8 on the stage
local YAW = 270

-- Half Clawd's height at scale 1, from the mesh: his centre goes this far times SCALE
-- above the floor, so his feet are on it
local HALF_HEIGHT = 40

-- The avatar part Clawd lives in. A hat is never needed for anything else
local HOST = "Hat_Static"

-- FUSER's rig names its bones bone_*; bone_spine_2 is mid-chest
local BONES = { "bone_spine_2", "bone_spine_1", "bone_pelvis", "char_root" }

-- Slot name to sRGB colour, as modelled
local COLOURS = {
    Mat_D77757 = { 0xD7, 0x77, 0x57 },
    Mat_000000 = { 0x00, 0x00, 0x00 },
    Mat_43484D = { 0x43, 0x48, 0x4D },
    Mat_808080 = { 0x80, 0x80, 0x80 },
}

-- Not BP_CaptureCharacter_C: FUSER renders those to make outfit and profile thumbnails
-- when the character editor closes, and with Clawd in one it crashed doing so
local AVATARS = {
    "BP_StageAvatar_C", "BP_AvatarPreviewCharacter_C", "BP_BattleStageAvatar_C",
    "BP_MultiplayerLobbyCharacter_C", "BP_MultiplayerBattleWaitingForPlayersCharacter_C",
}

-- EAttachmentRule and EComponentMobility
local KEEP_WORLD, SNAP_TO_TARGET, MOVABLE = 1, 2, 2
-- EVisibilityBasedAnimTickOption: keep the hidden body animating
local ALWAYS_TICK_POSE = 0

local said = {}

-- Each message once: the poll runs every second, and a problem would repeat with it
local function log(fmt, ...)
    local line = string.format("[Clawd] " .. fmt .. "\n", ...)
    if not said[line] then
        said[line] = true
        print(line)
    end
end

local function valid(obj)
    return obj ~= nil and obj:IsValid()
end

local function linear(c)
    c = c / 255
    return c <= 0.04045 and c / 12.92 or ((c + 0.055) / 1.055) ^ 2.4
end

-- Names are added, not only found: FName() alone can hand back None for a name the game
-- has never used, which a mod's own asset path and slot names are
local function name(s)
    return FName(s, EFindName.FNAME_Add)
end

local function statics(class)
    return StaticFindObject("/Script/Engine.Default__" .. class)
end

-- By object path through AssetRegistryHelpers, as UE4SS's BPModLoaderMod loads pak mods.
-- LoadAsset looks in the asset registry, which a shipping game builds from its own paks
-- and never learns a mod's assets from
local function loadAsset(path)
    local found = StaticFindObject(path)
    if valid(found) then
        return found
    end
    local helpers = StaticFindObject("/Script/AssetRegistry.Default__AssetRegistryHelpers")
    return helpers:GetAsset({ ObjectPath = name(path) })
end

local mesh

local function load()
    if valid(mesh) then
        return true
    end
    mesh = loadAsset(MESH)
    if not valid(mesh) then
        log("could not load %s; is Clawd_P.pak in Paks/~mods?", MESH)
        return false
    end
    log("loaded %s", mesh:GetFullName())
    return true
end

-- Calls f on each object in an array UE4SS returned, a TArray or a plain Lua table
local function each(array, f)
    if array == nil then
        return
    end
    local function object(e)
        local ok, v = pcall(function() return e:get() end)
        return (ok and v) or e
    end
    if type(array) == "table" then
        for _, e in ipairs(array) do
            f(object(e))
        end
    else
        array:ForEach(function(_, e) f(object(e)) end)
    end
end

local function components(avatar, class)
    local found = {}
    each(avatar:K2_GetComponentsByClass(StaticFindObject("/Script/Engine." .. class)), function(comp)
        if valid(comp) then
            found[#found + 1] = comp
        end
    end)
    return found
end

local boneOf = {}  -- body address -> the bone Clawd hangs from on it, or false

local function pickBone(body)
    local have = {}
    for i = 0, body:GetNumBones() - 1 do
        have[body:GetBoneName(i):ToString()] = true
    end
    for _, bone in ipairs(BONES) do
        if have[bone] then
            return bone
        end
    end
    return nil
end

local function boneFor(body)
    local key = body:GetAddress()
    if boneOf[key] == nil then
        boneOf[key] = pickBone(body) or false
    end
    return boneOf[key] or nil
end

-- Made for each avatar and owned by it, so they live exactly as long as the part they
-- colour. A set cached once was owned by the first avatar, freed with it when the
-- menu's level unloaded, and handed to the next, which crashed FUSER
local function makeMaterials(owner)
    local base = loadAsset(BASE_MATERIAL)
    if not valid(base) then
        log("no %s, Clawd stays grey", BASE_MATERIAL)
        return {}
    end
    local lib = statics("KismetMaterialLibrary")
    local made = {}
    for slot, rgb in pairs(COLOURS) do
        local mid = lib:CreateDynamicMaterialInstance(owner, base, name("Clawd_" .. slot))
        if valid(mid) then
            mid:SetVectorParameterValue(name("Color"),
                { R = linear(rgb[1]), G = linear(rgb[2]), B = linear(rgb[3]), A = 1 })
            made[slot] = mid
        end
    end
    return made
end

local madeFor = {}  -- avatar address -> its materials, made once

-- Made once per avatar and reused. They have fixed names under the avatar, and making
-- them again gave new objects the names of ones the renderer was still drawing with,
-- which Unreal rebuilds in place: pressing F8 a few times crashed FUSER
local function materialsFor(avatar)
    local key = avatar:GetAddress()
    local made = madeFor[key]
    if made then
        local fine = true
        for _, mid in pairs(made) do
            fine = fine and valid(mid)
        end
        if fine then
            return made
        end
    end
    made = makeMaterials(avatar)
    madeFor[key] = made
    return made
end

local function host(avatar)
    for _, comp in ipairs(components(avatar, "StaticMeshComponent")) do
        if comp:GetFName():ToString() == HOST then
            return comp
        end
    end
    return nil
end

-- Where Clawd sits relative to his bone, as placed: part address -> plain numbers. The
-- game sets the host part's offset from its socket too, and left alone that put him in
-- the floor while he stayed attached to the right bone
local placed = {}

local function relative(part)
    local t = part:GetRelativeTransform()
    return {
        Translation = { X = t.Translation.X, Y = t.Translation.Y, Z = t.Translation.Z },
        Rotation = { X = t.Rotation.X, Y = t.Rotation.Y, Z = t.Rotation.Z, W = t.Rotation.W },
        Scale3D = { X = t.Scale3D.X, Y = t.Scale3D.Y, Z = t.Scale3D.Z },
    }
end

local function moved(a, b)
    local function far(p, q, keys, limit)
        for _, k in ipairs(keys) do
            if math.abs(p[k] - q[k]) > limit then
                return true
            end
        end
        return false
    end
    return far(a.Translation, b.Translation, { "X", "Y", "Z" }, 1)
        or far(a.Rotation, b.Rotation, { "X", "Y", "Z", "W" }, 0.01)
        or far(a.Scale3D, b.Scale3D, { "X", "Y", "Z" }, 0.01)
end

-- Turns the avatar's host part into Clawd, at the spine bone, facing the way the
-- avatar faces. Snapped onto the bone and then turned and dropped in world terms, so he
-- keeps that pose relative to the bone as it moves
-- Where Clawd stands and which way he faces: on the spine bone, his feet on the avatar's
-- floor, turned YAW from the avatar. Set outright, never nudged: attaching again to the
-- same bone does nothing, so a nudge after it stacked on the last placement, and every
-- outfit reset sank Clawd another step into the floor. His height comes from the
-- avatar's feet, not the bone, whose height changes with body type
local function place(avatar, body, part)
    local bone = boneFor(body)
    part:K2_AttachToComponent(body, name(bone or "None"), SNAP_TO_TARGET, KEEP_WORLD, KEEP_WORLD, false)
    local at = bone and body:GetSocketLocation(name(bone)) or avatar:K2_GetActorLocation()
    local floor = avatar:K2_GetActorLocation().Z
    local facing = avatar:K2_GetActorRotation()
    part:K2_SetWorldLocation({ X = at.X, Y = at.Y, Z = floor + HALF_HEIGHT * SCALE + LIFT }, false, {}, false)
    -- Turned as a step of its own: turned in the same call as the move, he faced the same
    -- way whatever YAW was
    part:K2_SetWorldRotation({ Pitch = 0, Yaw = facing.Yaw + YAW, Roll = 0 }, false, {}, false)
    part:SetWorldScale3D({ X = SCALE, Y = SCALE, Z = SCALE })
    placed[part:GetAddress()] = relative(part)
    return bone
end

-- Turns the avatar's host part into Clawd and places him
local function clawdify(avatar, body, part)
    part:SetMobility(MOVABLE)
    part:SetStaticMesh(mesh)
    for slot, mid in pairs(materialsFor(avatar)) do
        part:SetMaterialByName(name(slot), mid)
    end
    local bone = place(avatar, body, part)
    part:SetVisibility(true, false)
    log("Clawd on %s at bone %s", avatar:GetClass():GetFName():ToString(), tostring(bone))
end

-- Whether the host part still hangs from the body's spine bone, where Clawd put it
local function onBone(part, body)
    local parent = part:GetAttachParent()
    return valid(parent) and parent:GetAddress() == body:GetAddress()
        and part:GetAttachSocketName():ToString() == (boneFor(body) or "None")
end



local function tick()
    if not load() then
        return
    end
    for _, class in ipairs(AVATARS) do
        for _, avatar in ipairs(FindAllOf(class) or {}) do
            local body = valid(avatar) and avatar.BodyMeshCom
            if body and valid(body) then
                local part = host(avatar)
                if not part then
                    log("%s has no %s to put Clawd in", class, HOST)
                else
                    -- The game resets the part whenever it applies an outfit: its mesh, and
                    -- where it hangs. Moved back to the body's root with his offset kept,
                    -- Clawd stood half in the floor
                    local current = part.StaticMesh
                    if not valid(current) or current:GetAddress() ~= mesh:GetAddress() then
                        clawdify(avatar, body, part)
                    elseif not onBone(part, body) then
                        log("%s moved Clawd off the bone; putting him back",
                            avatar:GetClass():GetFName():ToString())
                        clawdify(avatar, body, part)
                    else
                        local want, now = placed[part:GetAddress()], relative(part)
                        if want and moved(want, now) then
                            log("%s moved Clawd on the bone, to (%.0f, %.0f, %.0f) from (%.0f, %.0f, %.0f); putting him back",
                                avatar:GetClass():GetFName():ToString(),
                                now.Translation.X, now.Translation.Y, now.Translation.Z,
                                want.Translation.X, want.Translation.Y, want.Translation.Z)
                            part:K2_SetRelativeTransform(want, false, {}, false)
                        end
                    end
                    for _, comp in ipairs(components(avatar, "MeshComponent")) do
                        if comp:GetAddress() ~= part:GetAddress() and comp:IsVisible() then
                            comp:SetVisibility(false, false)
                        end
                    end
                    if not part:IsVisible() then
                        part:SetVisibility(true, false)
                    end
                    body.VisibilityBasedAnimTickOption = ALWAYS_TICK_POSE
                end
            end
        end
    end
end

-- F8 turns every Clawd a further 90 degrees and says the new YAW, to find his facing in
-- a new venue or view without restarting the game. It only turns him, and presses
-- closer than a third of a second apart are dropped
local lastTurn = 0
RegisterKeyBind(Key.F8, function()
    local now = os.clock()
    if now - lastTurn < 0.33 then
        return
    end
    lastTurn = now
    YAW = (YAW + 90) % 360
    print(string.format("[Clawd] YAW is now %d\n", YAW))
    ExecuteInGameThread(function()
        local ok, err = pcall(function()
            for _, class in ipairs(AVATARS) do
                for _, avatar in ipairs(FindAllOf(class) or {}) do
                    local body = valid(avatar) and avatar.BodyMeshCom
                    local part = body and valid(body) and host(avatar)
                    if part and valid(part.StaticMesh) and part.StaticMesh:GetAddress() == mesh:GetAddress() then
                        place(avatar, body, part)
                    end
                end
            end
        end)
        if not ok then
            print("[Clawd] turning: " .. tostring(err) .. "\n")
        end
    end)
end)

LoopAsync(1000, function()
    ExecuteInGameThread(function()
        local ok, err = pcall(tick)
        if not ok then
            log("%s", tostring(err))
        end
    end)
    return false
end)

log("loaded")
