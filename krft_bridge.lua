-- Kingdom Rush Frontiers Trainer Bridge
-- Target: Kingdom Rush Frontiers (KR2) desktop, Windows/Steam, LÖVE + LuaJIT
-- Loaded with: Kingdom Rush Frontiers.exe -custom_script krft_bridge
--
-- 架构与 KR1 版一致：外部 GUI 写状态文件，本脚本在游戏自己的 LuaJIT 里读状态并改运行时数值。
-- 不注入 DLL、不读写进程内存、不修改游戏安装目录。

local BRIDGE_VERSION = "1.2.0"
local STATE_FILE = "krft_state.lua"
local STATUS_FILE = "krft_status.txt"
local HEARTBEAT_TIMEOUT = 6
local STATUS_INTERVAL = 0.75
local STATE_POLL_INTERVAL = 0.20
local NIL = {}

local ok_signal, signal = pcall(require, "hump.signal")
local ok_storage, storage = pcall(require, "storage")

-- Frontiers 的 game_settings 在 kr2/ 下，require 名字与 KR1 相同。
local ok_gs, GS = pcall(require, "game_settings")

local M = {}
custom_script = M

local config = {
    active = false,
    heartbeat = 0,
    revision = 0,
    -- 速度（默认唯一启用项）
    speed_enabled = true,
    speed = 1,
    paused = false,
    -- 资源
    gold_enabled = false,
    gold_value = 99999,
    gems_enabled = false,
    gems_value = 99999,
    lives_enabled = false,
    lives_value = 20,
    kill_gold_enabled = false,
    kill_gold_multiplier = 2,
    -- 生存 / 战斗
    invincible = false,
    hero_no_cooldown = false,
    damage_enabled = false,
    damage_multiplier = 3,
    tower_speed_enabled = false,
    tower_speed_multiplier = 3,
    tower_range_enabled = false,
    tower_range_multiplier = 2,
    -- 兵营
    barrack_enabled = false,
    barrack_soldiers = 3,
    barrack_respawn_scale = 1.0,
    -- 进度
    unlock_levels = false,
    three_stars = false,
    -- 只读模式：完全不写游戏存档（见下方 read_only_active 说明）
    read_only = false,
    cmd_skip_wave = 0,
    cmd_force_wave = 0,
    cmd_win = 0
}

local poll_acc = 0
local status_acc = 0
local speed_fraction_acc = 0
local last_skip_wave = 0
local last_force_wave = 0
local last_win = 0
local current_store = nil

local speed_owner = nil
-- speed_original 按对象索引（同一局可能同时存在 active_item 与全局 game 两个表）
local speed_original = nil
local pause_owner = nil
local pause_original = NIL
local gold_owner = nil
local gold_original = NIL
local lives_owner = nil
local lives_original = NIL
local slot_snapshot = nil
local slot_snapshot_idx = nil

-- 弱引用分组：记录被改字段的原始值，用于恢复
local groups = {}

local function weak_group(name)
    if not groups[name] then
        groups[name] = setmetatable({}, {__mode = "k"})
    end
    return groups[name]
end

local function remember_and_set(group_name, obj, key, value)
    if type(obj) ~= "table" or type(obj[key]) ~= "number" then
        return
    end
    local group = weak_group(group_name)
    local fields = group[obj]
    if not fields then
        fields = {}
        group[obj] = fields
    end
    if fields[key] == nil then
        fields[key] = obj[key]
    end
    obj[key] = value
end

-- 记录原值后按 factor 缩放（factor=1 等价还原）
local function scale_field(group_name, obj, key, factor)
    if type(obj) ~= "table" or type(obj[key]) ~= "number" then
        return
    end
    local group = weak_group(group_name)
    local fields = group[obj]
    if not fields then
        fields = {}
        group[obj] = fields
    end
    if fields[key] == nil then
        fields[key] = obj[key]
    end
    obj[key] = fields[key] * factor
end

local function restore_group(group_name)
    local group = groups[group_name]
    if not group then
        return
    end
    for obj, fields in pairs(group) do
        if type(obj) == "table" then
            for key, value in pairs(fields) do
                obj[key] = value
            end
        end
    end
    groups[group_name] = setmetatable({}, {__mode = "k"})
end

local function restore_all_groups()
    restore_group("god")
    restore_group("hero_cd")
    restore_group("damage")
    restore_group("tower_speed")
    restore_group("tower_range")
    restore_group("barrack_count")
    restore_group("barrack_respawn")
end

local function deep_copy(value, seen)
    if type(value) ~= "table" then
        return value
    end
    seen = seen or {}
    if seen[value] then
        return seen[value]
    end
    local result = {}
    seen[value] = result
    for key, item in pairs(value) do
        result[deep_copy(key, seen)] = deep_copy(item, seen)
    end
    return result
end

local function clamp_number(value, minimum, maximum, fallback)
    value = tonumber(value)
    if not value then
        return fallback
    end
    if value < minimum then
        value = minimum
    elseif value > maximum then
        value = maximum
    end
    return value
end

-- 取当前活跃的 game item（KR1/KR2 均为 main.handler.active_item）
local function get_game()
    if type(main) == "table" and type(main.handler) == "table" then
        local item = main.handler.active_item
        if type(item) == "table" then
            if item.item_name == "game" and type(item.store) == "table" then
                return item
            end
            return nil
        end
    end
    if type(game) == "table" and type(game.store) == "table" then
        return game
    end
    return nil
end

local function active_store()
    if type(current_store) == "table" then
        return current_store
    end
    local g = get_game()
    if g and type(g.store) == "table" then
        return g.store
    end
    return nil
end

-- 杀敌金币倍率：监听 got-enemy-gold，在官方发放后补差额。
-- Frontiers 的 systems.lua 里同样是：
--   store.player_gold = store.player_gold + e.enemy.gold
--   signal.emit("got-enemy-gold", e, e.enemy.gold)
local kill_gold_hook_ok = false

local function ensure_kill_gold_hook()
    if kill_gold_hook_ok then
        return
    end
    if not ok_signal or type(signal) ~= "table" or type(signal.register) ~= "function" then
        return
    end
    local handler = function(_entity, amount)
        if not config.active or not config.kill_gold_enabled then
            return
        end
        local factor = tonumber(config.kill_gold_multiplier)
        if not factor or factor <= 1 then
            return
        end
        local base = tonumber(amount) or 0
        if base <= 0 then
            return
        end
        local store = active_store()
        if type(store) ~= "table" or type(store.player_gold) ~= "number" then
            return
        end
        local bonus = math.floor(base * (factor - 1) + 0.5)
        if bonus > 0 then
            store.player_gold = store.player_gold + bonus
        end
    end
    kill_gold_hook_ok = pcall(signal.register, "got-enemy-gold", handler)
end

local function read_state()
    if not (love and love.filesystem and love.filesystem.load) then
        return nil, "love.filesystem unavailable"
    end
    local loader, err = love.filesystem.load(STATE_FILE)
    if not loader then
        return nil, err
    end
    local ok, result = pcall(loader)
    if not ok or type(result) ~= "table" then
        return nil, result
    end
    return result, nil
end

local function refresh_config()
    local incoming = read_state()
    if type(incoming) ~= "table" then
        -- 状态文件读失败（GUI 恰好在 os.replace 的瞬间被读到半截文件等），
        -- 不要立刻停用：保留上一帧配置继续生效，等下一次轮询（0.2s 后）重试。
        -- 只有心跳真正超时才恢复原值。
        if os.time() - (tonumber(config.heartbeat) or 0) > HEARTBEAT_TIMEOUT then
            config.active = false
        end
        return
    end
    for key, value in pairs(incoming) do
        config[key] = value
    end
    local heartbeat = tonumber(config.heartbeat) or 0
    if os.time() - heartbeat > HEARTBEAT_TIMEOUT then
        config.active = false
    end
end

-- 速度：游戏自带 DBG_TIME_MULT（all/game.lua 的 time warp 调试通道）
--
-- 该字段挂在「游戏场景对象」上。不同版本 / 不同入口下这个对象可能是
-- main.handler.active_item，也可能是全局 game，因此两个都写，读回时以第一个为准。
local function speed_targets(g)
    local targets = {}
    if type(g) == "table" then
        targets[#targets + 1] = g
    end
    if type(game) == "table" and game ~= g then
        targets[#targets + 1] = game
    end
    return targets
end

local function restore_speed()
    if speed_original then
        -- 逐条还原：原值可能是 nil，pairs 会跳过 nil 值，故用数组存 [owner, value]
        for _, pair in ipairs(speed_original) do
            local owner, value = pair[1], pair[2]
            if type(owner) == "table" then
                owner.DBG_TIME_MULT = value
            end
        end
    end
    speed_owner = nil
    speed_original = nil
    speed_fraction_acc = 0
end

local function apply_speed(g)
    if not config.active or not config.speed_enabled then
        restore_speed()
        return
    end
    local targets = speed_targets(g)
    if speed_owner ~= g then
        restore_speed()
        speed_owner = g
        speed_original = {}
        for _, owner in ipairs(targets) do
            speed_original[#speed_original + 1] = { owner, owner.DBG_TIME_MULT }
        end
    end
    local requested = clamp_number(config.speed, 1, 16, 1)
    local whole = math.floor(requested)
    local fraction = requested - whole
    -- 小数倍率用相邻整数逐帧交替，取长期平均速度
    speed_fraction_acc = speed_fraction_acc + fraction
    if speed_fraction_acc >= 1 then
        whole = whole + 1
        speed_fraction_acc = speed_fraction_acc - 1
    end
    for _, owner in ipairs(targets) do
        owner.DBG_TIME_MULT = math.max(1, whole)
    end
end

local function restore_pause()
    if pause_owner and type(pause_owner) == "table" then
        if pause_original == NIL then
            pause_owner.paused = nil
        else
            pause_owner.paused = pause_original
        end
    end
    pause_owner = nil
    pause_original = NIL
end

local function apply_pause(store)
    if not config.active or not config.paused then
        restore_pause()
        return
    end
    if pause_owner ~= store then
        restore_pause()
        pause_owner = store
        if store.paused == nil then
            pause_original = NIL
        else
            pause_original = store.paused
        end
    end
    store.paused = true
end

local function restore_scalar(owner, original, field)
    if owner and type(owner) == "table" then
        if original == NIL then
            owner[field] = nil
        else
            owner[field] = original
        end
    end
end

local function apply_gold(store)
    if not config.active or not config.gold_enabled then
        restore_scalar(gold_owner, gold_original, "player_gold")
        gold_owner = nil
        gold_original = NIL
        return
    end
    if gold_owner ~= store then
        restore_scalar(gold_owner, gold_original, "player_gold")
        gold_owner = store
        gold_original = store.player_gold == nil and NIL or store.player_gold
    end
    store.player_gold = math.floor(clamp_number(config.gold_value, 0, 999999999, 99999))
end

local function apply_lives(store)
    if not config.active or not config.lives_enabled then
        restore_scalar(lives_owner, lives_original, "lives")
        lives_owner = nil
        lives_original = NIL
        return
    end
    if lives_owner ~= store then
        restore_scalar(lives_owner, lives_original, "lives")
        lives_owner = store
        lives_original = store.lives == nil and NIL or store.lives
    end
    store.lives = math.floor(clamp_number(config.lives_value, 1, 9999, 20))
end

-- 递归缩放 cooldown 字段（英雄技能 / 防御塔攻速共用）
local function recursive_scale_cooldowns(group_name, value, divisor, depth, visited)
    if type(value) ~= "table" or depth < 0 or visited[value] then
        return
    end
    visited[value] = true
    for key, item in pairs(value) do
        if type(item) == "table" then
            recursive_scale_cooldowns(group_name, item, divisor, depth - 1, visited)
        elseif type(item) == "number" and (key == "cooldown" or string.match(tostring(key), "_cooldown$")) then
            local group = weak_group(group_name)
            local fields = group[value]
            if not fields then
                fields = {}
                group[value] = fields
            end
            if fields[key] == nil then
                fields[key] = item
            end
            value[key] = divisor == 0 and 0 or fields[key] / divisor
        end
    end
end

local function set_range_fields(entity, multiplier)
    local function scale(obj, key)
        if type(obj) == "table" and type(obj[key]) == "number" then
            scale_field("tower_range", obj, key, multiplier)
        end
    end
    scale(entity.attacks, "range")
    scale(entity.melee, "range")
    scale(entity.ranged, "range")
    scale(entity.ranged, "range_while_blocking")
    local lists = {
        entity.attacks and entity.attacks.list,
        entity.melee and entity.melee.attacks,
        entity.ranged and entity.ranged.attacks
    }
    for _, list in ipairs(lists) do
        if type(list) == "table" then
            for _, attack in pairs(list) do
                scale(attack, "max_range")
                scale(attack, "min_range")
                scale(attack, "range")
            end
        end
    end
end

local function set_damage_fields(entity, multiplier)
    local function scale(obj, key)
        if type(obj) == "table" and type(obj[key]) == "number" then
            scale_field("damage", obj, key, multiplier)
        end
    end
    scale(entity.tower, "damage_factor")
    scale(entity.unit, "damage_factor")
    local lists = {
        entity.attacks and entity.attacks.list,
        entity.melee and entity.melee.attacks,
        entity.ranged and entity.ranged.attacks
    }
    for _, list in ipairs(lists) do
        if type(list) == "table" then
            for _, attack in pairs(list) do
                scale(attack, "damage_min")
                scale(attack, "damage_max")
            end
        end
    end
end

local function apply_entities(store)
    if type(store.entities) ~= "table" then
        return
    end
    local invincible = config.active and config.invincible
    local no_cooldown = config.active and config.hero_no_cooldown
    local damage_enabled = config.active and config.damage_enabled
    local tower_speed_enabled = config.active and config.tower_speed_enabled
    local tower_range_enabled = config.active and config.tower_range_enabled
    local damage_multiplier = clamp_number(config.damage_multiplier, 0.1, 100, 3)
    local speed_multiplier = clamp_number(config.tower_speed_multiplier, 0.1, 50, 3)
    local range_multiplier = clamp_number(config.tower_range_multiplier, 0.1, 20, 2)

    for _, entity in pairs(store.entities) do
        if type(entity) == "table" then
            local friendly = entity.tower or entity.hero or entity.soldier
            if invincible and friendly and entity.health and not entity.health.dead
                and type(entity.health.hp_max) == "number" then
                remember_and_set("god", entity.health, "hp", entity.health.hp_max)
            end
            if no_cooldown and entity.hero then
                recursive_scale_cooldowns("hero_cd", entity, 0, 5, {})
            end
            if tower_speed_enabled and entity.tower then
                recursive_scale_cooldowns("tower_speed", entity, speed_multiplier, 4, {})
            end
            if tower_range_enabled and entity.tower then
                set_range_fields(entity, range_multiplier)
            end
            if damage_enabled and friendly then
                set_damage_fields(entity, damage_multiplier)
            end
        end
    end

    if not invincible then restore_group("god") end
    if not no_cooldown then restore_group("hero_cd") end
    if not damage_enabled then restore_group("damage") end
    if not tower_speed_enabled then restore_group("tower_speed") end
    if not tower_range_enabled then restore_group("tower_range") end
end

-- 兵营：出怪人数 entity.barrack.max_soldiers（原版 3）
--       刷新时间 entity.health.dead_lifetime（士兵阵亡后等该秒数才补兵）
local function apply_barracks(store)
    if not config.active or not config.barrack_enabled or type(store.entities) ~= "table" then
        restore_group("barrack_count")
        restore_group("barrack_respawn")
        return
    end
    -- 下限与 GUI 的 Spinbox(from_=1) 保持一致；设为 0 会让兵营完全不出兵
    local count = math.floor(clamp_number(config.barrack_soldiers, 1, 10, 3))
    local scale = clamp_number(config.barrack_respawn_scale, 0.1, 5.0, 1.0)
    for _, entity in pairs(store.entities) do
        if type(entity) == "table" and type(entity.barrack) == "table" and entity.tower then
            remember_and_set("barrack_count", entity.barrack, "max_soldiers", count)
            local soldiers = entity.barrack.soldiers
            if type(soldiers) == "table" then
                for _, s in pairs(soldiers) do
                    if type(s) == "table" and type(s.health) == "table" then
                        scale_field("barrack_respawn", s.health, "dead_lifetime", scale)
                    end
                end
            end
        end
    end
end

-- 只读模式（read_only = true）
--
-- 目的：完全不触碰存档文件，只做运行时内存修改。
-- 适用场景：存档目录不可写、存档由第三方工具管理、或用户不希望修改器动存档。
--
-- 只读模式下：
--   * 宝石 / 关卡解锁 / 全关三星 三项**强制关闭**，并在状态文件里回报原因
--   * restore_slot_snapshot() 直接返回，永不调用 save_slot
--   * 速度、金币、生命、伤害、射程、兵营、暂停、波次等纯运行时功能**不受影响**
--     （它们本来就不写存档）
--
-- 注意：桥接脚本本身仍要写 krft_state.lua / krft_status.txt，
-- 那两个是修改器自有文件，与游戏存档无关。
local function read_only_active()
    return config.active and config.read_only == true
end

local function restore_slot_snapshot()
    -- 只读模式下绝不动存档：即使内存里还留着快照也直接丢弃
    if read_only_active() then
        slot_snapshot = nil
        slot_snapshot_idx = nil
        return
    end
    if not slot_snapshot or not ok_storage then
        slot_snapshot = nil
        slot_snapshot_idx = nil
        return
    end
    local ok, slot = pcall(function()
        return storage:load_slot(slot_snapshot_idx)
    end)
    if ok and type(slot) == "table" then
        slot.gems = slot_snapshot.gems
        slot.levels = deep_copy(slot_snapshot.levels or {})
        pcall(function()
            storage:save_slot(slot, slot_snapshot_idx, true)
        end)
    end
    slot_snapshot = nil
    slot_snapshot_idx = nil
end

-- 关卡总数：优先读 game_settings.last_level，回退到已知的 22（Frontiers 主线）
local function last_level_index()
    if ok_gs then
        local n = tonumber(GS.last_level)
        if n and n >= 1 then
            return math.floor(n)
        end
    end
    return 22
end

local function apply_slot_features()
    -- 只读模式下三项存档功能一律不执行
    if read_only_active() then
        if slot_snapshot then
            slot_snapshot = nil
            slot_snapshot_idx = nil
        end
        return
    end
    local needs_slot = config.active and (config.gems_enabled or config.unlock_levels or config.three_stars)
    if not needs_slot or not ok_storage or not storage.active_slot_idx then
        if slot_snapshot and not needs_slot then
            restore_slot_snapshot()
        end
        return
    end
    local idx = storage.active_slot_idx
    if slot_snapshot and slot_snapshot_idx ~= idx then
        restore_slot_snapshot()
    end
    local ok, slot = pcall(function()
        return storage:load_slot(idx)
    end)
    if not ok or type(slot) ~= "table" then
        return
    end
    if not slot_snapshot then
        slot_snapshot_idx = idx
        slot_snapshot = {
            gems = slot.gems,
            levels = deep_copy(slot.levels or {})
        }
    end
    if config.gems_enabled then
        slot.gems = math.floor(clamp_number(config.gems_value, 0, 999999999, 99999))
    end
    if config.unlock_levels or config.three_stars then
        slot.levels = slot.levels or {}
        local last_level = last_level_index()
        for i = 1, last_level do
            if type(slot.levels[i]) ~= "table" then
                slot.levels[i] = {}
            end
            if config.three_stars then
                slot.levels[i].stars = 3
            end
        end
    end
end

local function command_skip_wave(store)
    local nonce = tonumber(config.cmd_skip_wave) or 0
    if nonce > last_skip_wave then
        last_skip_wave = nonce
        store.send_next_wave = true
    end
end

local function command_force_wave(store)
    local nonce = tonumber(config.cmd_force_wave) or 0
    if nonce > last_force_wave then
        last_force_wave = nonce
        store.force_next_wave = true
    end
end

-- 立即通关：Frontiers 的 game.lua 在 store.game_outcome 非空时跳过生命检查
-- （原版调试串 "Lives checking OFF (store.game_outcome set)"），并监听
-- game-victory / game-victory-after。
-- 立即通关
--
-- 官方胜利流程（all/systems.lua 的关卡协程结尾）依次是：
--     run_complete  ->  写 slot_level / stars / already_won  ->  save_slot
--                    ->  emit "game-victory"  ->  emit "game-victory-after"
-- 也就是说「解锁下一关」是靠协程正常跑到结尾时顺手写进存档的。
--
-- 早期版本在这里直接构造 store.game_outcome 并补发两个信号，等于跳过了整段协程收尾，
-- 结果能显示胜利画面却不写存档、不解锁下一关。
--
-- 现在改为驱动游戏自己的判定：杀掉全部敌人 + 标记波次打完 + 强制推进，
-- 让 systems.lua 的胜利分支自然触发，走完整条官方流程。
local win_stage = nil      -- 0=待执行 1=已清场 2=已标记波次
local win_store = nil

local function kill_all_enemies(store)
    if type(store.entities) ~= "table" then
        return
    end
    -- 只改血量与 dead 标志，交给引擎自己的死亡处理与 has_alive_enemies 判定。
    -- 不发任何自定义信号：FRONTiers 里并不存在 "enemies-killed" 这类信号，
    -- 凭空 emit 会引入无法预期的影响。
    for _, entity in pairs(store.entities) do
        if type(entity) == "table" and entity.enemy and type(entity.health) == "table" then
            if not entity.health.dead then
                entity.health.hp = 0
                entity.health.dead = true
            end
        end
    end
end

local function mark_waves_finished(store)
    -- 官方胜利判定看两件事：没有存活敌人 + 波次已打完（has_alive_enemies / waves_finished）。
    -- 这里只温和地推进一步并请求引擎送波，由引擎自己把波次走完，
    -- 不用 9999 之类的魔法值去污染关卡状态（会影响无尽模式与统计）。
    if type(store.waves_finished) == "number" then
        store.waves_finished = store.waves_finished + 1
    end
    store.send_next_wave = true
    store.force_next_wave = true
end

local function command_instant_win(store)
    local nonce = tonumber(config.cmd_win) or 0
    if nonce <= last_win then
        return
    end
    last_win = nonce
    if store.game_outcome then
        return
    end
    win_stage = 0
    win_store = store
end

-- 分帧推进：先清场，下一帧再标记波次完成，
-- 让引擎有机会跑一次正常的敌人清理与波次检查，最终由 systems.lua 判定胜利。
--
-- 重要：只有 command_instant_win（即用户点击「立即通关」）才允许把 win_stage 推进。
-- 换关时必须保持 win_stage = nil，否则每次载入新关卡都会自动清场并判定通关。
local function process_instant_win(store)
    if not config.active or store.game_outcome then
        win_stage = nil
        win_store = nil
        return
    end
    -- 换关：只清空状态，绝不启动通关流程
    if win_store ~= store then
        if win_store ~= nil then
            -- 已经启动过流程却换了关卡，放弃本次请求
            win_stage = nil
        end
        win_store = store
        return
    end
    -- 未点击按钮 -> 不做任何事
    if win_stage == nil then
        return
    end
    if win_stage == 0 then
        kill_all_enemies(store)
        win_stage = 1
    elseif win_stage == 1 then
        mark_waves_finished(store)
        win_stage = 2
    else
        -- 已推进到标记阶段；若仍未判定胜利（关卡脚本未收尾），持续施压
        if not store.game_outcome then
            kill_all_enemies(store)
            store.force_next_wave = true
        else
            win_stage = nil
            win_store = nil
        end
    end
end

local function restore_runtime()
    restore_speed()
    restore_pause()
    restore_scalar(gold_owner, gold_original, "player_gold")
    gold_owner = nil
    gold_original = NIL
    restore_scalar(lives_owner, lives_original, "lives")
    lives_owner = nil
    lives_original = NIL
    restore_all_groups()
end

local function write_status(g)
    local scene = g and "game" or "menu"
    local slot_idx = ok_storage and storage.active_slot_idx or 0
    local level_idx = 0
    if g and type(g.store) == "table" and type(g.store.level_idx) == "number" then
        level_idx = g.store.level_idx
    end
    local lines = {
        "bridge=1",
        "bridge_version=" .. BRIDGE_VERSION,
        "active=" .. (config.active and "1" or "0"),
        "scene=" .. scene,
        "slot=" .. tostring(slot_idx or 0),
        "level=" .. tostring(level_idx),
        "revision=" .. tostring(config.revision or 0),
        "restored=" .. (config.active and "0" or "1"),
        "kill_gold_hook=" .. (kill_gold_hook_ok and "1" or "0"),
        "barrack=" .. (config.barrack_enabled and "1" or "0"),
        "read_only=" .. (config.read_only and "1" or "0"),
        -- 只读模式下三项存档功能的强制关闭回报，便于界面提示用户
        "slot_writable=" .. ((read_only_active()) and "0" or "1"),
        "speed=" .. tostring(clamp_number(config.speed, 1, 16, 1)),
        "last_level=" .. tostring(last_level_index()),
        "win_stage=" .. tostring(win_stage or -1),
        "game_outcome=" .. ((g and g.store and g.store.game_outcome) and "1" or "0"),
        "timestamp=" .. tostring(os.time())
    }
    pcall(function()
        love.filesystem.write(STATUS_FILE, table.concat(lines, "\n") .. "\n")
    end)
end

function M:init()
    refresh_config()
    ensure_kill_gold_hook()
    write_status(get_game())
end

function M:game_init()
    current_store = nil
    gold_owner = nil
    gold_original = NIL
    lives_owner = nil
    lives_original = NIL
    pause_owner = nil
    pause_original = NIL
    win_stage = nil
    win_store = nil
    restore_all_groups()
end

function M:update(dt)
    dt = tonumber(dt) or 0
    poll_acc = poll_acc + dt
    status_acc = status_acc + dt

    if poll_acc >= STATE_POLL_INTERVAL then
        poll_acc = 0
        refresh_config()
    end

    local g = get_game()
    if not config.active then
        restore_runtime()
        current_store = nil
    else
        apply_slot_features()
        if g and g.store then
            local store = g.store
            if current_store ~= store then
                restore_pause()
                restore_scalar(gold_owner, gold_original, "player_gold")
                restore_scalar(lives_owner, lives_original, "lives")
                restore_all_groups()
                current_store = store
                gold_owner = nil
                gold_original = NIL
                lives_owner = nil
                lives_original = NIL
                -- 换关时清掉立即通关的分帧状态，避免残留到下一局
                win_stage = nil
                win_store = nil
            end
            apply_speed(g)
            apply_pause(store)
            apply_gold(store)
            apply_lives(store)
            apply_entities(store)
            apply_barracks(store)
            command_skip_wave(store)
            command_force_wave(store)
            command_instant_win(store)
            process_instant_win(store)
        else
            restore_speed()
            restore_pause()
            restore_scalar(gold_owner, gold_original, "player_gold")
            gold_owner = nil
            gold_original = NIL
            restore_scalar(lives_owner, lives_original, "lives")
            lives_owner = nil
            lives_original = NIL
            restore_all_groups()
            current_store = nil
            win_stage = nil
            win_store = nil
        end
    end

    if status_acc >= STATUS_INTERVAL then
        status_acc = 0
        write_status(g)
    end
end

function M:keypressed(key, isrepeat)
    -- 快捷键由外部 GUI 处理；保留回调以满足引擎契约
end

return M
