"""Game-agnostic force-feedback bridge for the Logitech G27.

An *adapter* (see g27bridge/apps/) reads telemetry from one game and hands
the bridge a Frame per update. The bridge owns everything else: the wheel,
shaping the force (strength, invert, smoothing, damping, deadband lift),
the idle centering spring, rev LEDs, live keys, the status line and config.

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
from dataclasses import dataclass
from typing import Optional

from .g27 import G27, G27Error, rpm_leds

# Repo root / config / <app>.json
CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")

# Settings every app gets. Adapters add their own on top via Adapter.defaults.
CORE_DEFAULTS = {
    "range": 540,              # wheel rotation lock-to-lock, degrees (40-900)
    "strength": 1.0,           # overall gain
    "invert": False,           # flip if the wheel pushes away from centre
    "smoothing": 0.3,          # 0 = raw, towards 1 = smoother but laggier
    "damping": 0.05,           # resists fast wheel movement; calms oscillation
    "min_force": 0.04,         # lifts small forces past the G27's gear deadband
    "idle_autocenter": 0.3,    # spring when the game isn't sending (menus, pause)
    "leds": True,              # rev LEDs on the wheel
    "timeout_ms": 250,         # no telemetry for this long -> idle
}


@dataclass
class Frame:
    """One telemetry update from an adapter.

    force:     wheel torque request where +/-1.0 is full torque at strength 1,
               already in the G27's sign convention. None means the game gives
               no usable force right now; the bridge holds a centering spring.
    steer_vel: steering velocity (any consistent unit) for damping, or 0.
    rpm_frac:  engine rpm / redline (0..1) for the rev LEDs, or None.
    """
    force: Optional[float]
    steer_vel: float = 0.0
    rpm_frac: Optional[float] = None


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
        """Return the newest Frame since the last call, or None. Must not block
        for more than ~10 ms; the bridge calls it in a tight loop."""
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


def load_config(path, defaults, legacy_path=None):
    cfg = dict(CORE_DEFAULTS)
    cfg.update(defaults)
    src = path if os.path.exists(path) else legacy_path
    if src and os.path.exists(src):
        with open(src) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


class ForceProcessor:
    """Shapes an adapter's force request into a -1..1 wheel command."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.smoothed = 0.0
        self.clipped = False

    def reset(self):
        self.smoothed = 0.0

    def process(self, force, steer_vel=0.0):
        c = self.cfg
        f = force * c["strength"]
        if c["invert"]:
            f = -f
        a = min(max(c["smoothing"], 0.0), 0.95)
        self.smoothed += (1.0 - a) * (f - self.smoothed)
        out = self.smoothed - c["damping"] * steer_vel
        self.clipped = abs(out) > 1.0
        out = max(-1.0, min(1.0, out))
        if abs(out) > 0.005:  # skip the deadband lift near zero to avoid buzzing
            mf = c["min_force"]
            out = math.copysign(mf + (1.0 - mf) * abs(out), out)
        return out


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
        self.log.append(("autocenter", f))

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


def run(adapter, wheel, cfg_path, max_seconds=None):
    cfg = adapter.cfg
    proc = ForceProcessor(cfg)
    keys = Keys()

    cfg["range"] = wheel.set_range(cfg["range"])
    wheel.set_autocenter(cfg["idle_autocenter"])
    where = adapter.open()

    active = False
    spring_on = True
    last_rx = 0.0
    last_status = 0.0
    last_led = 0.0
    out = 0.0
    frame = None
    note = ""
    started = time.monotonic()

    print(f"Listening for {adapter.title}{' on ' + where if where else ''}. Start driving.")
    print("Keys: +/- strength  [/] damping  ,/. range  i invert  l LEDs  s save  q quit\n")
    try:
        while True:
            now = time.monotonic()
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

            # --- telemetry ---
            newest = adapter.poll()
            if newest:
                frame = newest
                last_rx = now
                if not active:
                    active = True
                    proc.reset()
                    adapter.reset()
                # No usable force from the game -> keep the centering spring.
                want_spring = frame.force is None
                if want_spring != spring_on:
                    wheel.set_autocenter(cfg["idle_autocenter"] if want_spring else 0)
                    spring_on = want_spring
                if frame.force is not None:
                    out = proc.process(frame.force, frame.steer_vel)
                    wheel.set_force(out)
                else:
                    out = 0.0
                    wheel.set_force(0)
                if cfg["leds"] and frame.rpm_frac is not None and now - last_led > 0.03:
                    wheel.set_leds(rpm_leds(frame.rpm_frac))
                    last_led = now
            elif active and now - last_rx > cfg["timeout_ms"] / 1000.0:
                active = False
                out = 0.0
                wheel.stop_force()
                wheel.set_leds(0)
                if not spring_on:
                    wheel.set_autocenter(cfg["idle_autocenter"])
                    spring_on = True

            # --- status line ---
            if now - last_status > 0.1:
                last_status = now
                common = (f"str {cfg['strength']:.1f}  damp {cfg['damping']:.2f}"
                          f"  range {cfg['range']}  inv {_onoff(cfg['invert'])}"
                          f"  leds {_onoff(cfg['leds'])}")
                if active and frame:
                    line = (f"{adapter.status(frame)}  out {out * 100:5.0f}%"
                            f"{' CLIP' if proc.clipped else '     '}  {common}")
                else:
                    line = f"waiting for game...  {common}"
                if note:
                    line += f"  [{note}]"
                    note = ""
                sys.stdout.write("\r" + line.ljust(118))
                sys.stdout.flush()
    finally:
        adapter.close()


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
        run(adapter, wheel, path, max_seconds=args.seconds)
    except KeyboardInterrupt:
        pass
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
