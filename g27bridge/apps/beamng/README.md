# BeamNG.drive

The BeamNG mod (`g27bridge.zip`, source in `mod/`) reads the steering-rack force of the car you're driving and sends it to the bridge on your own PC (UDP, `127.0.0.1:4445`). The BeamNG side uses documented modding hooks. Two engine calls are tried with fallbacks.

Do the general setup in the [main README](../../../README.md) first (driver removal, Python install).

## 1. Install the mod

Copy `g27bridge/apps/beamng/g27bridge.zip` (don't unzip it) into your BeamNG user folder's `mods` folder. The launcher's *Manage User Folder → Open in Explorer* takes you there. Make sure the mod is enabled in-game under *Mods → Mods Manager*.

If you edit the Lua in `mod/`, re-zip it so that `lua/` is at the top of the zip.

## 2. Bind the controls

Run the bridge first (step 3) so the wheel is in native mode before you bind anything. Native mode gives separate pedal axes, the clutch and the shifter. Then, in *Options → Controls → Bindings*:

- **Steering**: the wheel axis. Set *Steering wheel rotation* to the same range the bridge uses (default 540°). **Turn force feedback off** on this binding; the bridge supplies it.
- **Throttle / Brake / Clutch**: each pedal's own axis. If a pedal reads full when released, tick *Inverted*.
- **H-shifter**: under the gearbox bindings, bind each gear (1–6, R) to its shifter position. Push the shifter down to select reverse. Choose a manual gearbox in the vehicle, or set the transmission mode to one that accepts a H-shifter.

## 3. Drive

```
python -m g27bridge beamng          (or double-click start_beamng.bat)
```

The status line shows `raw` (rack force from the game) and `peak` before the common values. A good starting point is setting `full_scale` to about 70% of the peaks you see in normal driving.

## BeamNG settings (`config/beamng.json`)

These come on top of the [common settings](../../../README.md#settings-configgamejson-created-when-you-press-s).

| Setting | Default | Meaning |
|---|---|---|
| `full_scale` | 2000 | rack force that gives 100% torque at strength 1 |
| `port` | 4445 | UDP port the mod sends to (the mod's port is fixed in its Lua) |

A `config.json` saved by older versions in the repo root is still read until you save a new `config/beamng.json`.

## Troubleshooting

- **Status stays on `waiting for game...`**: the mod isn't loaded or isn't sending. Check it's enabled, then press `Ctrl+R` to reload the vehicle. Open the console (`~`) and look for lines tagged `g27bridge`.
- **Status shows `NO RACK (spring only)`**: this vehicle steers in a way the mod can't read. You'll get a plain centering spring for that car. The console message says how many steering hydros it found.
