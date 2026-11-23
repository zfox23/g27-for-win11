"""Game-agnostic force-feedback bridge for the Logitech G27.

An *adapter* (see g27bridge/apps/) reads telemetry from one game and hands
the bridge a Frame per update. The bridge owns everything else: the wheel,
a fixed-rate (~500 Hz) control loop that shapes the force (strength, invert,
smoothing, damping on the wheel's measured velocity, deadband lift), the
crossfaded centering spring, rev LEDs, live keys, the status line and config.

Live keys (in the console window):
    +/-   strength          [/]   damping
    ,/.   steering range      i   invert force direction
    l     rev LEDs on/off     s   save settings
    q     quit
"""
import argparse
import json
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from . import g27
from .g27 import G27, G27Error, rpm_leds

# Repo root / config / <app>.json
CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")

# Settings every app gets. Adapters add their own on top via Adapter.defaults.
CORE_DEFAULTS = {
    "range": 540,              # wheel rotation lock-to-lock, degrees (40-900)
    "strength": 1.0,           # overall gain
    "invert": False,           # flip if the wheel pushes away from centre
    "smoothing_ms": 3.0,       # low-pass on the game force (~its delay in ms); 0 = raw
    "damping": 0.05,           # torque fraction per 270 deg/s of wheel speed
    "min_force": 0.04,         # lifts small forces past the G27's gear deadband
    "idle_autocenter": 0.3,    # spring when the game isn't sending (menus, pause)
    "fade_ms": 150,            # crossfade between game force and the spring
    "leds": True,              # rev LEDs on the wheel
    "timeout_ms": 250,         # no telemetry for this long -> idle
}

TICK_S = 0.002               # control loop period (~500 Hz)
DAMPING_REF_DPS = 270.0      # damping = torque fraction at this wheel speed
MIN_FORCE_KNEE = 0.02        # below this the deadband lift ramps in linearly
VEL_WINDOW_S = 0.008         # differentiate wheel angle over at least this span
VEL_TAU_S = 0.005            # low-pass on wheel velocity
VEL_DEADZONE_DPS = 3.0       # ignore encoder jitter below this speed
VEL_STALE_S = 0.03           # no position report for this long -> wheel is still
SPRING_SPEED_FULL = 15.0     # m/s at which a rack-less car's spring is full strength
SPRING_SEND_S = 0.025        # min gap between hardware spring updates
SPRING_STEP = 0.02           # min spring change worth sending


@dataclass
class Frame:
    """One telemetry update from an adapter.

    force:     wheel torque request where +/-1.0 is full torque at strength 1,
               already in the G27's sign convention. None means the game gives
               no usable force right now; the bridge holds a centering spring.
    steer_vel: steering velocity in steering-input units (-1..1 lock to lock)
               per second. Only used for damping if the wheel's own position
               can't be read over HID.
    rpm_frac:  engine rpm / redline (0..1) for the rev LEDs, or None.
    speed:     vehicle speed in m/s, or None. Firms up the fallback spring.
    """
    force: Optional[float]
    steer_vel: float = 0.0
    rpm_frac: Optional[float] = None
    speed: Optional[float] = None


class Adapter:
    """Base class for a game adapter. Subclass it in g27bridge/apps/<name>/."""

    name = "app"            # used for the config file name: config/<name>.json
    title = "the game"      # shown in messages, e.g. "Listening for BeamNG ..."
    defaults = {}           # app-specific settings, merged over CORE_DEFAULTS

    def __init__(self, cfg):
        self.cfg = cfg

    def open(self):
        """Start listening (open sockets, map shared memory, ...). Return a
        one-line description of where it's listening, for the console."""
        return ""

    def poll(self):
        """Return the newest Frame since the last call, or None. Must not
        block: the bridge calls it every ~2 ms from its control loop."""
        raise NotImplementedError

    def reset(self):
        """Called when telemetry resumes after an idle period."""

    def status(self, frame):
        """Short app-specific text shown at the start of the status line."""
        return ""

    def close(self):
        pass


def config_path(app_name):
    return os.path.join(CONFIG_DIR, f"{app_name}.json")


def smoothing_to_ms(a, rate_hz=120.0):
    """Old per-packet EMA factor (0..0.95) -> equivalent time constant in ms."""
    a = min(max(float(a), 0.0), 0.95)
    return 0.0 if a <= 0 else round(-1000.0 / rate_hz / math.log(a), 1)


def load_config(path, defaults, legacy_path=None):
    cfg = dict(CORE_DEFAULTS)
    cfg.update(defaults)
    src = path if os.path.exists(path) else legacy_path
    if src and os.path.exists(src):
        with open(src) as f:
            saved = json.load(f)
        # Settings files from before the fixed-rate loop stored a per-packet factor.
        if "smoothing" in saved:
            old = saved.pop("smoothing")
            saved.setdefault("smoothing_ms", smoothing_to_ms(old))
        cfg.update(saved)
    return cfg


def save_config(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def lift(x, min_force, knee=MIN_FORCE_KNEE):
    """Deadband compensation: map |x| in (0, 1] to (0, 1] so that any real
    request clears the G27's gear deadband. Continuous and monotonic: linear
    through zero up to the knee, then min_force + (1 - min_force) * |x|."""
    mf = min(max(min_force, 0.0), 0.5)
    ax = abs(x)
    if ax >= knee:
        y = mf + (1.0 - mf) * ax
    else:
        y = ax * (mf + (1.0 - mf) * knee) / knee
    return math.copysign(min(y, 1.0), x)


class WheelState:
    """Wheel angular velocity in deg/s, measured from HID position reports
    (preferred) or, if the wheel never reports, from the game's steer_vel."""

    def __init__(self):
        self.samples = deque(maxlen=128)  # (time, degrees)
        self.vel = 0.0
        self.have_hid = False

    def add_hid(self, raw, t, range_deg):
        self.samples.append((t, g27.angle_deg(raw, range_deg)))
        self.have_hid = True

    def _hid_velocity(self, now):
        if not self.samples or now - self.samples[-1][0] > VEL_STALE_S:
            return 0.0  # reports stop when the wheel stops moving
        t1, d1 = self.samples[-1]
        t0, d0 = self.samples[0]
        for t, d in reversed(self.samples):
            if t1 - t >= VEL_WINDOW_S:
                t0, d0 = t, d
                break
        return (d1 - d0) / (t1 - t0) if t1 > t0 else 0.0

    def update(self, now, dt, game_steer_vel, range_deg):
        raw = (self._hid_velocity(now) if self.have_hid
               else game_steer_vel * range_deg / 2.0)
        self.vel += (1.0 - math.exp(-dt / VEL_TAU_S)) * (raw - self.vel)
        v = abs(self.vel) - VEL_DEADZONE_DPS
        return math.copysign(v, self.vel) if v > 0 else 0.0

    @property
    def source(self):
        return "hid" if self.have_hid else "game"


class ForceProcessor:
    """Shapes the game's force request and the damping into a -1..1 wheel
    command, once per control tick."""

    def __init__(self, cfg, axis_sign):
        self.cfg = cfg
        self.axis_sign = axis_sign  # see g27.load_axis_sign; 0 disables damping
        self.s1 = self.s2 = 0.0
        self.game = 0.0
        self.damp = 0.0
        self.clipped = False

    def reset(self, force=0.0):
        """Start the filter settled on a value (in output units after
        strength/invert), so a fade-in is a pure gain ramp."""
        self.s1 = self.s2 = force

    def shape(self, force):
        c = self.cfg
        f = force * c["strength"]
        return -f if c["invert"] else f

    def process(self, dt, force, vel_dps, gain):
        """force: game request (None = hold the last value, e.g. while fading
        out); vel_dps: wheel velocity; gain: 0..1 crossfade on the game force."""
        c = self.cfg
        if force is not None:
            f = self.shape(force)
            # Critically damped 2-pole low-pass, time constant split across the
            # two stages. Runs every tick, so it also turns the game's 60-120 Hz
            # steps into a smooth signal, and it behaves the same at any packet rate.
            tau = max(float(c["smoothing_ms"]), 0.0) / 1000.0 / 2.0
            if tau > 0:
                a = 1.0 - math.exp(-dt / tau)
                self.s1 += a * (f - self.s1)
                self.s2 += a * (self.s1 - self.s2)
            else:
                self.s1 = self.s2 = f
        self.game = self.s2 * gain
        # Viscous damping opposes the wheel's motion in its own axis.
        self.damp = -self.axis_sign * c["damping"] * vel_dps / DAMPING_REF_DPS
        out = self.game + self.damp
        self.clipped = abs(out) > 1.0
        out = max(-1.0, min(1.0, out))
        return lift(out, c["min_force"]) if out else 0.0


class SpringRamp:
    """Moves the G27's hardware centering spring towards a target at a limited
    rate, sending updates only when they're worth it."""

    def __init__(self, wheel, level):
        self.wheel = wheel
        self.level = level
        self.sent = level
        self.last_send = -1.0

    def update(self, now, dt, target, rate):
        step = rate * dt
        self.level += max(-step, min(step, target - self.level))
        lvl = 0.0 if self.level < 0.005 else self.level
        if lvl == self.sent or now - self.last_send < SPRING_SEND_S:
            return
        if abs(lvl - self.sent) >= SPRING_STEP or lvl == target or lvl == 0.0:
            self.wheel.set_autocenter(lvl)
            self.sent = lvl
            self.last_send = now


class FakeWheel:
    """Stands in for the G27 with --dry-run."""

    def __init__(self):
        self.force = 0.0
        self.log = []

    def set_range(self, d):
        d = max(40, min(900, int(d)))
        self.log.append(("range", d))
        return d

    def set_autocenter(self, f):
        self.log.append(("autocenter", round(f, 3)))

    def read_position(self):
        return None

    def set_force(self, f):
        self.force = f

    def stop_force(self):
        self.force = 0.0

    def set_leds(self, m):
        pass

    def close(self):
        pass


class Keys:
    """Non-blocking single-key reads (Windows console only)."""

    def __init__(self):
        try:
            import msvcrt
            self.m = msvcrt
        except ImportError:
            self.m = None

    def poll(self):
        out = []
        while self.m and self.m.kbhit():
            ch = self.m.getwch()
            if ch in ("\x00", "\xe0"):  # arrow/function key prefix
                self.m.getwch()
                continue
            out.append(ch)
        return out


def _onoff(b):
    return "on " if b else "off"


def run(adapter, wheel, cfg_path, axis_sign, max_seconds=None):
    cfg = adapter.cfg
    proc = ForceProcessor(cfg, axis_sign)
    state = WheelState()
    keys = Keys()

    cfg["range"] = wheel.set_range(cfg["range"])
    wheel.set_autocenter(cfg["idle_autocenter"])
    spring = SpringRamp(wheel, cfg["idle_autocenter"])
    where = adapter.open()

    active = False        # telemetry arrived within timeout_ms
    gain = 0.0            # crossfade on the game force, 0..1
    last_rx = 0.0
    last_status = 0.0
    last_led = 0.0
    out = 0.0
    vel = 0.0
    frame = None
    note = ""
    hz, ticks, hz_t0 = 0.0, 0, time.perf_counter()
    started = prev = next_tick = time.perf_counter()

    print(f"Listening for {adapter.title}{' on ' + where if where else ''}. Start driving.")
    print("Keys: +/- strength  [/] damping  ,/. range  i invert  l LEDs  s save  q quit\n")
    try:
        while True:
            now = time.perf_counter()
            dt = min(max(now - prev, 0.0005), 0.02)
            prev = now
            if max_seconds and now - started > max_seconds:
                break

            # --- keys ---
            for ch in keys.poll():
                if ch in "+=":
                    cfg["strength"] = round(min(cfg["strength"] + 0.1, 5.0), 2)
                elif ch in "-_":
                    cfg["strength"] = round(max(cfg["strength"] - 0.1, 0.0), 2)
                elif ch == "]":
                    cfg["damping"] = round(min(cfg["damping"] + 0.01, 1.0), 3)
                elif ch == "[":
                    cfg["damping"] = round(max(cfg["damping"] - 0.01, 0.0), 3)
                elif ch in ".>":
                    cfg["range"] = wheel.set_range(cfg["range"] + 30)
                elif ch in ",<":
                    cfg["range"] = wheel.set_range(cfg["range"] - 30)
                elif ch in "iI":
                    cfg["invert"] = not cfg["invert"]
                elif ch in "lL":
                    cfg["leds"] = not cfg["leds"]
                    if not cfg["leds"]:
                        wheel.set_leds(0)
                    note = f"LEDs {'on' if cfg['leds'] else 'off'}"
                elif ch in "sS":
                    save_config(cfg_path, cfg)
                    note = "saved"
                elif ch in "qQ":
                    return

            # --- inputs ---
            newest = adapter.poll()
            if newest:
                frame = newest
                last_rx = now
                if not active:
                    active = True
                    adapter.reset()
                if cfg["leds"] and frame.rpm_frac is not None and now - last_led > 0.03:
                    wheel.set_leds(rpm_leds(frame.rpm_frac))
                    last_led = now
            elif active and now - last_rx > cfg["timeout_ms"] / 1000.0:
                active = False
                wheel.set_leds(0)
            pos = wheel.read_position()
            if pos:
                state.add_hid(pos[0], pos[1], cfg["range"])

            # --- crossfade between game force and the centering spring ---
            game_force = frame.force if (active and frame) else None
            idle = cfg["idle_autocenter"]
            if game_force is not None:
                gain_target, spring_target = 1.0, 0.0
            elif active and frame.speed is not None:
                # Rack-less car: the spring stands in for aligning torque, which
                # builds with speed.
                k = min(max(frame.speed, 0.0) / SPRING_SPEED_FULL, 1.0)
                gain_target, spring_target = 0.0, idle * (0.3 + 0.7 * k)
            else:
                gain_target, spring_target = 0.0, idle
            fade_s = max(cfg["fade_ms"], 1) / 1000.0
            if gain == 0.0 and game_force is not None:
                proc.reset(proc.shape(game_force))  # settle on the current force, not a stale one
            gain += max(-dt / fade_s, min(dt / fade_s, gain_target - gain))
            spring.update(now, dt, spring_target, max(idle, 0.1) / fade_s)

            # --- force ---
            steer_vel = frame.steer_vel if (active and frame) else 0.0
            vel = state.update(now, dt, steer_vel, cfg["range"])
            out = proc.process(dt, game_force, vel, gain)
            wheel.set_force(out)

            # --- status line ---
            ticks += 1
            if now - hz_t0 >= 1.0:
                hz, ticks, hz_t0 = ticks / (now - hz_t0), 0, now
            if now - last_status > 0.1:
                last_status = now
                damp_src = state.source if axis_sign else "uncal"
                common = (f"str {cfg['strength']:.1f}  damp {cfg['damping']:.2f} ({damp_src})"
                          f"  range {cfg['range']}  inv {_onoff(cfg['invert'])}"
                          f"  leds {_onoff(cfg['leds'])}  {hz:3.0f}Hz")
                if active and frame:
                    line = (f"{adapter.status(frame)}  out {out * 100:5.0f}%"
                            f"{' CLIP' if proc.clipped else '     '}  {common}")
                else:
                    line = f"waiting for game...  {common}"
                if note:
                    line += f"  [{note}]"
                    note = ""
                sys.stdout.write("\r" + line.ljust(130))
                sys.stdout.flush()

            # --- wait for the next tick ---
            next_tick += TICK_S
            wait = next_tick - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            elif wait < -0.05:
                next_tick = time.perf_counter()  # fell far behind; don't burst
    finally:
        adapter.close()


def resolve_axis_sign(wheel, dry_run):
    """Force direction relative to the wheel axis, for damping. Calibrates once
    (and saves config/wheel.json) if it isn't known yet. 0 = unknown."""
    if dry_run:
        return g27.load_axis_sign() or 1
    sign = g27.load_axis_sign()
    if sign:
        return sign
    print("First run: calibrating force direction for damping.")
    print("Hands off the wheel: it will turn a little each way in 2 s...")
    time.sleep(2.0)
    try:
        sign = g27.calibrate(wheel)
    except G27Error as e:
        print(f"Calibration failed: {e}\nDamping is off until "
              "'python -m g27bridge wheel calibrate' succeeds.")
        return 0
    g27.save_axis_sign(sign)
    return sign


def add_bridge_args(p):
    p.add_argument("--dry-run", action="store_true", help="no wheel; just show values")
    p.add_argument("--config", help="settings file (default: config/<app>.json)")
    p.add_argument("--range", type=int, help="override steering range for this session")
    p.add_argument("--force", action="store_true", help="skip G27 revision check")
    p.add_argument("--seconds", type=float, help=argparse.SUPPRESS)  # testing


def main(adapter_cls, args, legacy_config=None):
    """Load config, open the wheel, and run the bridge with one adapter."""
    path = args.config or config_path(adapter_cls.name)
    cfg = load_config(path, adapter_cls.defaults,
                      legacy_path=None if args.config else legacy_config)
    if args.range:
        cfg["range"] = args.range
    adapter = adapter_cls(cfg)

    try:
        wheel = FakeWheel() if args.dry_run else G27.open(force=args.force)
    except G27Error as e:
        sys.exit(f"Error: {e}")

    try:
        with g27.timer_resolution():
            axis_sign = resolve_axis_sign(wheel, args.dry_run)
            run(adapter, wheel, path, axis_sign, max_seconds=args.seconds)
    except KeyboardInterrupt:
        pass
    except G27Error as e:
        sys.exit(f"\nError: {e}")
    except OSError as e:
        sys.exit(f"\nError: {e} (is another copy of the bridge already running?)")
    finally:
        try:
            wheel.stop_force()
            wheel.set_leds(0)
            wheel.set_autocenter(cfg["idle_autocenter"])
        except Exception:
            pass
        wheel.close()
        print("\nStopped. Wheel returned to its centering spring.")
