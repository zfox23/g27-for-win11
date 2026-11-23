# G27 Bridge: force feedback for the Logitech G27 on Windows 11, no Logitech drivers

This lets a Logitech G27 work in sims on Windows 11 **with Memory Integrity left on**. Nothing here installs or runs a kernel driver.

- **Inputs** (steering, all three pedals, H-shifter, buttons) go through Windows' own built-in HID driver.
- **Force feedback, steering lock and rev LEDs** are sent straight to the wheel by the bridge over USB HID.
- **Each game** gets a small adapter that reads its telemetry and tells the bridge how hard to push.

Supported games:

| Game | Command | Setup |
|---|---|---|
| BeamNG.drive | `python -m g27bridge beamng` (or `start_beamng.bat`) | [g27bridge/apps/beamng/README.md](g27bridge/apps/beamng/README.md) |

> **Status: untested on real hardware.** The G27 commands come from the Linux `hid-lg4ff` driver. Each part was tested against simulated input. Expect to tune strength and direction on first use.

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
python -m g27bridge wheel info
```
`info` should list the wheel (in *compatibility mode* until you run the bridge for the first time after power-up).

Optional check: `python -m g27bridge wheel test-ffb` pushes the wheel briefly one way, then the other.

On its first run the bridge **calibrates the force direction** used for damping: take your hands off the wheel when it says so, and it turns a little each way. The result goes in `config/wheel.json`. Run `python -m g27bridge wheel calibrate` to redo it.

## 3. Set up your game

Follow the setup page for your game in the table above. Then run its bridge and leave it running while you play. It switches the wheel to native mode, sets the range, and waits for the game.

## Bridge keys and status (all games)

| Key | Action |
|---|---|
| `+` / `-` | strength ±0.1 |
| `[` / `]` | damping ±0.01 (raise if the wheel oscillates on straights or feels too light when you let go) |
| `,` / `.` | steering range ±30° (change the game's steering rotation to match) |
| `i` | invert force direction |
| `l` | rev LEDs on/off |
| `s` | save settings to `config/<game>.json` |
| `q` | quit (the wheel goes back to a gentle centering spring) |

**First drive, check the direction:** in a corner, the wheel should pull back towards centre. If it pushes further into the turn, press **`i`**, then **`s`**.

The status line starts with game-specific values, then shows `out` (wheel torque %). If `CLIP` shows often, lower strength. After `damp`, `(hid)` means damping uses the wheel's own measured speed, `(game)` means the game's steering input (fallback), and `(uncal)` means damping is off until calibration succeeds. The last value is the control loop rate (about 500 Hz).

**How the force is built:** the bridge runs a fixed ~500 Hz loop. It smooths the game's force with a short low-pass filter, which also turns the game's 60–120 Hz updates into a smooth signal. It adds viscous damping from the wheel's angle, read straight from the wheel over HID, then lifts small forces past the G27's gear deadband without a step at centre. When the game starts or stops sending (or a car has no readable rack), the game force and the wheel's centering spring crossfade instead of snapping. For cars without a rack, the spring firms up with speed.

`--dry-run` runs any bridge without a wheel. `--range N` overrides the range for one session.

## Settings (`config/<game>.json`, created when you press `s`)

Every game has its own settings file. These settings apply to all of them. Game-specific settings are listed on each game's page.

| Setting | Default | Meaning |
|---|---|---|
| `range` | 540 | rotation lock-to-lock in degrees |
| `strength` | 1.0 | overall gain |
| `invert` | false | flip force direction |
| `smoothing_ms` | 3 | low-pass on the game force, roughly its added delay in ms; 0 = raw, higher = smoother but less road detail |
| `damping` | 0.05 | torque fraction per 270°/s of wheel speed (same feel at any range) |
| `min_force` | 0.04 | lifts small forces past the G27's gear deadband |
| `idle_autocenter` | 0.3 | spring strength in menus or when paused (rack-less cars: 30% at standstill, rising to this at 15 m/s) |
| `fade_ms` | 150 | crossfade time between game force and the spring |
| `leds` | true | rev LEDs |
| `timeout_ms` | 250 | no telemetry for this long → idle (spring) |

Settings files saved by older versions with `smoothing` (a 0–0.95 factor) are converted to `smoothing_ms` automatically. The old default 0.3 becomes about 7 ms; set it to 3 for the current, more responsive default.

`config/wheel.json` holds the wheel's calibration (`force_axis_sign`) and is shared by all games.

## Wheel commands

`python -m g27bridge wheel <command>` controls the wheel without running a bridge:

- `info`: list connected G27s and their mode
- `native`: switch to native mode (separate pedal axes, clutch, shifter, 900°)
- `range 900`: set the steering lock (40 to 900)
- `autocenter 30`: centering spring in %, 0 = off
- `test-ffb`: short push each way
- `monitor`: show the wheel position the bridge reads over HID (left should read low, right high; `--range N` sets the range while monitoring)
- `calibrate`: learn which way positive force turns the wheel, so damping always resists motion (hands off the wheel)

## Troubleshooting (all games)

- **`No G27 found`**: check the power adapter. Run `python -m g27bridge wheel info`.
- **`HID write failed`**: close G HUB, LGS or any other wheel utility.
- **Pedals all on one axis**: the wheel is still in compatibility mode. Start the bridge (or run `python -m g27bridge wheel native`), then rebind in the game.
- **After unplugging the wheel or power-cycling it**, it starts in compatibility mode again. Restart the bridge.

## Repo layout

```
g27bridge/
  core/
    g27.py            G27 HID commands (native mode, range, spring, force, LEDs, position, calibration)
    bridge.py         game-agnostic bridge: 500 Hz force loop, damping, spring crossfade, keys, config
  apps/
    __init__.py       registry of games
    beamng/
      adapter.py      reads the mod's UDP packets
      mod/            Lua source of the BeamNG mod
      g27bridge.zip   the packed mod to drop into BeamNG's mods folder
config/               your saved settings, one file per game, plus wheel.json (not in git)
start_beamng.bat      double-click launcher for BeamNG
```

## Adding a game

1. Create `g27bridge/apps/<name>/` with `__init__.py` and `adapter.py`.
2. In `adapter.py`, subclass `g27bridge.core.bridge.Adapter`:
   - `name`, `title`, and `defaults` (settings specific to this game, such as a port or force scale).
   - `open()`: start listening (socket, shared memory, ...). Return a short "where" string.
   - `poll()`: return the newest `Frame` or `None`. Must not block: it's called every ~2 ms.
   - `status(frame)`: optional text for the start of the status line.
   - `close()`.
3. Each `Frame` has `force` (±1.0 = full torque at strength 1, using the G27's sign convention; `None` = hold the centering spring), `steer_vel` (steering input −1..1 per second; only used for damping if the wheel position can't be read), `rpm_frac` (0–1 of redline, for LEDs) and `speed` (m/s, firms up the fallback spring).
4. Register it in `APPS` in `g27bridge/apps/__init__.py`. It then runs as `python -m g27bridge <name>`. Optionally add a `start_<name>.bat` alongside `start_beamng.bat`.

The BeamNG adapter (`g27bridge/apps/beamng/adapter.py`) is a complete example.
