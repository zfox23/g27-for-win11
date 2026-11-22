# G27 Bridge: BeamNG.drive force feedback for the Logitech G27, no Logitech drivers

This lets a Logitech G27 work in BeamNG.drive on Windows 11 **with Memory Integrity left on**. Nothing here installs or runs a kernel driver.

- **Inputs** (steering, all three pedals, H-shifter, buttons) go through Windows' own built-in HID driver.
- **Force feedback, steering lock and rev LEDs** are sent straight to the wheel by `bridge.py` over USB HID.
- **The BeamNG mod** (`g27bridge.zip`) reads the steering-rack force of the car you're driving and sends it to the bridge on your own PC (UDP, `127.0.0.1:4445`).

> **Status: untested on real hardware.** The G27 commands come from the Linux `hid-lg4ff` driver. The BeamNG side uses documented modding hooks, and two engine calls are tried with fallbacks. Each part was tested against simulated input. Expect to tune strength and direction on first use (step 5).

---

## 1. Remove the old Logitech drivers and turn Memory Integrity back on

1. Uninstall **Logitech Gaming Software** (and Logitech Profiler if present) in *Settings → Apps*.
2. Open *Windows Security → Device security → Core isolation details*. If it lists incompatible drivers, note their names, which are usually Logitech `.sys` files.
3. Open **Terminal (Admin)** and run:
   ```
   pnputil /enum-drivers
   ```
   Find each entry whose *Original Name* matches a Logitech driver from step 2 and note its *Published Name* (`oemNN.inf`). Then remove each one:
   ```
   pnputil /delete-driver oemNN.inf /uninstall /force
   ```
4. Restart, turn **Memory integrity** on, and restart again.
5. Plug in the G27 (USB **and** its power adapter). Device Manager should show it under *Human Interface Devices* as an ordinary HID game controller.

## 2. Install the Python side

Install Python 3.9 or newer from python.org (tick "Add to PATH"). Then, in this folder:
```
pip install -r requirements.txt
python g27.py info
```
`info` should list the wheel (in *compatibility mode* until you run the bridge for the first time after power-up).

Optional check: `python g27.py test-ffb` pushes the wheel briefly one way, then the other.

## 3. Install the BeamNG mod

Copy `g27bridge.zip` (don't unzip it) into your BeamNG user folder's `mods` folder. The launcher's *Manage User Folder → Open in Explorer* takes you there. Make sure the mod is enabled in-game under *Mods → Mods Manager*.

## 4. Bind the controls in BeamNG

Run the bridge first (step 5) so the wheel is in native mode before you bind anything. Native mode gives separate pedal axes, the clutch and the shifter. Then, in *Options → Controls → Bindings*:

- **Steering**: the wheel axis. Set *Steering wheel rotation* to the same range the bridge uses (default 540°). **Turn force feedback off** on this binding; the bridge supplies it.
- **Throttle / Brake / Clutch**: each pedal's own axis. If a pedal reads full when released, tick *Inverted*.
- **H-shifter**: under the gearbox bindings, bind each gear (1–6, R) to its shifter position. Push the shifter down to select reverse. Choose a manual gearbox in the vehicle, or set the transmission mode to one that accepts a H-shifter.

## 5. Drive

```
python bridge.py          (or double-click start_bridge.bat)
```
Leave it running while you play. It switches the wheel to native mode, sets the range, and then waits for the game.

**First drive, check the direction:** in a corner, the wheel should pull back towards centre. If it pushes further into the turn, press **`i`** (invert), then **`s`** to save.

| Key | Action |
|---|---|
| `+` / `-` | strength ±0.1 |
| `[` / `]` | damping ±0.01 (raise if the wheel oscillates on straights) |
| `,` / `.` | steering range ±30° (change BeamNG's *Steering wheel rotation* to match) |
| `i` | invert force direction |
| `l` | rev LEDs on/off |
| `s` | save settings to `config.json` |
| `q` | quit (the wheel goes back to a gentle centering spring) |

The status line shows `raw` (rack force from the game), `peak`, and `out` (wheel torque %). If `CLIP` shows often, lower strength or raise `full_scale` in `config.json`. A good starting point is setting `full_scale` to about 70% of the peaks you see in normal driving.

To set the steering lock without running the bridge: `python g27.py range 900` (any value from 40 to 900).

## Settings (`config.json`, created when you press `s`)

| Setting | Default | Meaning |
|---|---|---|
| `range` | 540 | rotation lock-to-lock in degrees |
| `strength` | 1.0 | overall gain |
| `full_scale` | 2000 | rack force that gives 100% torque at strength 1 |
| `invert` | false | flip force direction |
| `smoothing` | 0.3 | 0 = raw; higher = smoother but laggier |
| `damping` | 0.05 | resists fast wheel movement |
| `min_force` | 0.04 | lifts small forces past the G27's gear deadband |
| `idle_autocenter` | 0.3 | spring strength in menus or when paused |
| `leds` | true | rev LEDs |

## Troubleshooting

- **`No G27 found`**: check the power adapter. Run `python g27.py info`.
- **`HID write failed`**: close G HUB, LGS or any other wheel utility.
- **Status stays on `waiting for game...`**: the mod isn't loaded or isn't sending. Check it's enabled, then press `Ctrl+R` to reload the vehicle. Open the console (`~`) and look for lines tagged `g27bridge`.
- **Status shows `NO RACK (spring only)`**: this vehicle steers in a way the mod can't read. You'll get a plain centering spring for that car. The console message says how many steering hydros it found.
- **Pedals all on one axis**: the wheel is still in compatibility mode. Start the bridge (or run `python g27.py native`), then rebind.
- **After unplugging the wheel or power-cycling it**, it starts in compatibility mode again. Restart the bridge.

## Files

- `g27.py`: G27 HID commands plus a small CLI (`info`, `native`, `range`, `autocenter`, `test-ffb`).
- `bridge.py`: the force-feedback bridge.
- `g27bridge.zip`: the BeamNG mod (source in `beamng_mod/`).
