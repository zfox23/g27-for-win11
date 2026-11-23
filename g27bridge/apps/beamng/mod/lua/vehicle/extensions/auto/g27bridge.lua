-- G27 Bridge for BeamNG.drive
--
-- Sends the steering-rack force of the vehicle you're driving to the G27
-- bridge over UDP (127.0.0.1:4445). The bridge (python -m g27bridge beamng)
-- turns it into G27 force feedback, so no Logitech driver is needed.
--
-- Packet (text): "G27B1 <rackForce> <steer> <steerVel> <rpm> <maxRpm> <speed> <rackBeams>"

local M = {}

local HOST, PORT = "127.0.0.1", 4445
local SEND_INTERVAL = 1 / 120
local TAG = "g27bridge"

local udp = nil
local rack = nil       -- list of { cid = beam id, sign = +1/-1 }
local forceApi = nil   -- name of the obj method that returns beam force
local engine = nil
local prevSteer = nil
local sendAcc = 0      -- time owed towards the next send (keeps the remainder)
local sinceSend = 0    -- actual time since the last send, for steerVel

local function loadSocket()
  for _, name in ipairs({ "libs/luasocket/socket.socket", "socket.socket", "socket" }) do
    local ok, s = pcall(require, name)
    if ok and type(s) == "table" and s.udp then return s end
  end
  return nil
end

local function beamValue(api, cid)
  local ok, val = pcall(function() return obj[api](obj, cid) end)
  if ok and type(val) == "number" and val == val then return val end
  return nil
end

local function findRack()
  rack = {}
  local hydros = (v and v.data and v.data.hydros) or {}
  for _, h in pairs(hydros) do
    if type(h) == "table" then
      local src = h.inputSource or "steering_input"
      if type(src) == "string" and src:find("steering", 1, true)
         and not src:find("rear", 1, true) and not src:find("rw_", 1, true) then
        local cid = h.cid or h.beamCid or (type(h.beam) == "table" and h.beam.cid) or nil
        if cid then
          local factor = tonumber(h.factor or h.outFactor) or 1
          table.insert(rack, { cid = cid, sign = factor < 0 and -1 or 1 })
        end
      end
    end
  end

  forceApi = nil
  if #rack > 0 then
    for _, api in ipairs({ "getBeamForce", "getBeamStress" }) do
      if beamValue(api, rack[1].cid) then forceApi = api break end
    end
  end

  local ok, eng = pcall(function() return powertrain.getDevice("mainEngine") end)
  engine = ok and eng or nil

  if forceApi then
    log("I", TAG, string.format("Found %d steering hydro(s), reading force via obj:%s", #rack, forceApi))
  else
    log("W", TAG, string.format("No usable steering hydros (found %d). FFB will fall back to the "
      .. "centering spring. This vehicle may steer with a different system.", #rack))
  end
end

local function onExtensionLoaded()
  local socket = loadSocket()
  if not socket then
    log("E", TAG, "LuaSocket not available; G27 bridge disabled.")
    return
  end
  udp = socket.udp()
  udp:settimeout(0)
end

local function onReset()
  prevSteer = nil
  sendAcc = 0
  sinceSend = 0
end

local function updateGFX(dt)
  if not udp then return end
  if playerInfo and playerInfo.firstPlayerSeated == false then return end
  if not rack then findRack() end

  sendAcc = sendAcc + dt
  sinceSend = sinceSend + dt
  if sendAcc < SEND_INTERVAL then return end
  -- Carry the remainder so the average rate is 120 Hz at any frame rate
  -- (resetting to 0 halved it at e.g. 144 fps). Below 120 fps, send every frame.
  sendAcc = sendAcc - SEND_INTERVAL
  if sendAcc >= SEND_INTERVAL then sendAcc = 0 end
  local step = sinceSend
  sinceSend = 0

  local force = 0
  if forceApi then
    for i = 1, #rack do
      local f = beamValue(forceApi, rack[i].cid)
      if f then force = force + f * rack[i].sign end
    end
  end

  local e = electrics.values
  local steer = e.steering_input or 0
  local steerVel = prevSteer and (steer - prevSteer) / step or 0
  prevSteer = steer
  local maxRpm = (engine and engine.maxRPM) or e.maxrpm or 0

  udp:sendto(string.format("G27B1 %.2f %.4f %.3f %.0f %.0f %.2f %d",
    force, steer, steerVel, e.rpm or 0, maxRpm, e.wheelspeed or 0,
    forceApi and #rack or 0), HOST, PORT)
end

M.onExtensionLoaded = onExtensionLoaded
M.onReset = onReset
M.updateGFX = updateGFX

return M
