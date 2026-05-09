local mp = require "mp"
local utils = require "mp.utils"

local state_path = mp.get_opt("exposure_curve_file")
local fps = tonumber(mp.get_opt("exposure_curve_fps") or "60") or 60
local interval = 1.0 / math.max(1, fps)
local points = {}
local enabled = true
local last_ev = nil
local duration = nil

local function split_line(line)
    local cols = {}
    for part in string.gmatch(line, "[^\t]+") do
        cols[#cols + 1] = part
    end
    return cols
end

local function load_curve()
    if not state_path then
        return
    end
    local file = io.open(state_path, "r")
    if not file then
        return
    end
    local loaded = {}
    local header = file:read("*l")
    if header then
        if string.sub(header, 1, 8) == "enabled=" then
            enabled = string.sub(header, 9) ~= "0"
        end
    end
    for line in file:lines() do
        local cols = split_line(line)
        local t = tonumber(cols[1])
        local ev = tonumber(cols[2])
        if t and ev then
            loaded[#loaded + 1] = { t = t, ev = ev }
        end
    end
    file:close()
    table.sort(loaded, function(a, b) return a.t < b.t end)
    points = loaded
    last_ev = nil
end

local function value_at(t)
    if #points == 0 then
        return 0
    end
    if t <= points[1].t then
        return points[1].ev
    end
    for i = 1, #points - 1 do
        local a = points[i]
        local b = points[i + 1]
        if t <= b.t then
            local span = math.max(0.001, b.t - a.t)
            local u = math.max(0, math.min(1, (t - a.t) / span))
            local s = u * u * (3 - 2 * u)
            return a.ev + (b.ev - a.ev) * s
        end
    end
    return points[#points].ev
end

local function set_ev(ev)
    ev = math.max(-4, math.min(4, ev))
    if last_ev and math.abs(ev - last_ev) < 0.0005 then
        return
    end
    mp.set_property("glsl-shader-opts", string.format("exposure=%.4f", ev))
    last_ev = ev
end

local timer = mp.add_periodic_timer(interval, function()
    if not enabled then
        return
    end
    local t = mp.get_property_number("time-pos")
    duration = duration or mp.get_property_number("duration")
    if duration and t and t >= duration - 0.25 then
        return
    end
    if t then
        set_ev(value_at(t))
    end
end)
timer:resume()

mp.register_script_message("reload-exposure-curve", function()
    load_curve()
    local t = mp.get_property_number("time-pos") or 0
    duration = mp.get_property_number("duration") or duration
    if enabled then
        set_ev(value_at(t))
    end
end)

load_curve()
duration = mp.get_property_number("duration")
