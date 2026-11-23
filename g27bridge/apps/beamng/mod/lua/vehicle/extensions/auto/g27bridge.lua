-- G27 Bridge for BeamNG.drive
--
-- Sends the steering force of the vehicle you're driving to the G27 bridge
-- over UDP (127.0.0.1:4445). The bridge (python -m g27bridge beamng) turns it
-- into G27 force feedback, so no Logitech driver is needed.
--
-- The force is computed the same way BeamNG's own FFB does it (lua/vehicle/
-- hydros.lua): how far the steering hydros' actual length lags the length the
-- steering input commands, in steering-input units. It's sampled on every
-- physics step (2 kHz) and averaged over each send interval, so the
-- high-frequency buzz of the stiff rack beam can't alias into the signal.
--
-- Packet (text): "G27B2 <ffb> <steer> <steerVel> <rpm> <maxRpm> <speed> <hydros>"
--   ffb: +1.0 = BeamNG's full FFB torque at its default strength (100),
--        positive pushes the wheel towards negative steering (left).

local M = {}

local HOST, PORT = "127.0.0.1", 4445
local SEND_INTERVAL = 1 / 120
local FFMAX = 10            -- hydros.lua: FFB force that means full torque
local REF_FORCE_COEF = 100  -- BeamNG's default FFB strength
local TAG = "g27bridge"

local udp = nil
local rack = nil       -- the steering hydros (entries of hydros.hydros)
local ffbCoef = 1.2    -- hydros.lua's vehicleFFBForceCoef
local engine = nil
local prevSteer = nil
local sendAcc = 0      -- time owed towards the next send (keeps the remainder)
local sinceSend = 0    -- actual time since the last send, for steerVel
local dispSum, dispCount = 0, 0  -- physics-step samples since the last send

local function loadSocket()
  for _, name in ipairs({ "libs/luasocket/socket.socket", "socket.socket", "socket" }) do
    local ok, s = pcall(require, name)
    if ok and type(s) == "table" and s.udp then return s end
  end
  return nil
end

-- Same as hydros.lua: hydro length ratio -> steering-input units.
local function toInputSpace(h, state)
  return (state - h.center) * (state > h.center and h.invMultOut or h.invMultIn)
end

local function findRack()
  rack = {}
  local list = (hydros and hydros.hydros) or {}
  for _, h in ipairs(list) do
    -- hydros.init normalises inputSource and fills in bcid, center, invMult*.
    if h.inputSource == "steering_input" and h.bcid and h.invMultOut and h.invMultIn
       and h.invFFBHydroRefL and h.fgetDisplacement and h.fIsBroken then
      table.insert(rack, h)
    end
  end

  local input = v and v.data and v.data.input
  ffbCoef = 1.2 * ((input and tonumber(input.FFBcoef)) or 1)

  local ok, eng = pcall(function() return powertrain.getDevice("mainEngine") end)
  engine = ok and eng or nil

  if #rack > 0 then
    log("I", TAG, string.format("Found %d steering hydro(s); FFB coef %.2f", #rack, ffbCoef))
  else
    log("W", TAG, "No steering hydros found. FFB will fall back to the centering "
      .. "spring. This vehicle may steer with a different system.")
  end
end

-- How far the rack lags the commanded steering, averaged over the hydros.
local function rackDisplacement()
  local sum, n = 0, 0
  for i = 1, #rack do
    local h = rack[i]
    if not h.fIsBroken(obj, h.bcid) then
      local actual = toInputSpace(h, h.fgetDisplacement(obj, h.bcid) * h.invFFBHydroRefL)
      sum = sum + toInputSpace(h, h.state) - actual
      n = n + 1
    end
  end
  if n == 0 then return nil end
  return sum / n
end

local function onExtensionLoaded()
  local socket = loadSocket()
  if not socket then
    log("E", TAG, "LuaSocket not available; G27 bridge disabled.")
    return
  end
  udp = socket.udp()
  udp:settimeout(0)
  enablePhysicsStepHook()
end

local function onReset()
  rack = nil  -- hydros.init may have rebuilt its list
  prevSteer = nil
  sendAcc = 0
  sinceSend = 0
  dispSum, dispCount = 0, 0
end

local function onPhysicsStep(dtPhys)
  if not rack then return end
  local d = rackDisplacement()
  if d and d == d then
    dispSum = dispSum + d
    dispCount = dispCount + 1
  end
end

local function updateGFX(dt)
  if not udp then return end
  if playerInfo and playerInfo.firstPlayerSeated == false then return end
  if not rack then findRack() end

  sendAcc = sendAcc + dt
  sinceSend = sinceSend + dt
  if sendAcc < SEND_INTERVAL then return end
  -- Carry the remainder so the average rate is 120 Hz at any frame rate.
  -- Below 120 fps, send every frame.
  sendAcc = sendAcc - SEND_INTERVAL
  if sendAcc >= SEND_INTERVAL then sendAcc = 0 end
  local step = sinceSend
  sinceSend = 0

  -- No physics steps since the last send means the game is paused: send
  -- nothing, so the bridge falls back to its idle spring.
  if #rack > 0 and dispCount == 0 then return end

  local ffb = 0
  if dispCount > 0 then
    ffb = REF_FORCE_COEF * ffbCoef * (dispSum / dispCount) / FFMAX
  end
  dispSum, dispCount = 0, 0

  local e = electrics.values
  local steer = e.steering_input or 0
  local steerVel = prevSteer and (steer - prevSteer) / step or 0
  prevSteer = steer
  local maxRpm = (engine and engine.maxRPM) or e.maxrpm or 0

  udp:sendto(string.format("G27B2 %.5f %.4f %.3f %.0f %.0f %.2f %d",
    ffb, steer, steerVel, e.rpm or 0, maxRpm, e.wheelspeed or 0, #rack), HOST, PORT)
end

M.onExtensionLoaded = onExtensionLoaded
M.onReset = onReset
M.onPhysicsStep = onPhysicsStep
M.updateGFX = updateGFX

return M
