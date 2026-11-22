"""BeamNG.drive -> Logitech G27 force-feedback bridge.

Receives steering-rack force from the g27bridge BeamNG mod over UDP and
drives the G27's motors directly over HID. No Logitech drivers needed.

    python bridge.py              # run with the wheel
    python bridge.py --dry-run    # no wheel: just show what would be sent

Live keys (in this console window):
    +/-   strength          [/]   damping
    ,/.   steering range      i   invert force direction
    l     rev LEDs on/off     s   save settings
    q     quit
"""
import argparse
import json
import math
import os
import socket
import sys
import time

from g27 import G27, G27Error, rpm_leds

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "port": 4445,
    "range": 540,              # wheel rotation lock-to-lock, degrees (40-900)
    "strength": 1.0,           # overall gain
    "full_scale": 2000.0,      # rack force that equals 100% wheel torque at strength 1
    "invert": False,           # flip if the wheel pushes away from centre
    "smoothing": 0.3,          # 0 = raw, towards 1 = smoother but laggier
    "damping": 0.05,           # resists fast wheel movement; calms oscillation
    "min_force": 0.04,         # lifts small forces past the G27's gear deadband
    "idle_autocenter": 0.3,    # spring when the game isn't sending (menus, pause)
    "leds": True,              # rev LEDs on the wheel
    "timeout_ms": 250,         # no packets for this long -> idle
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(path, cfg):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def parse_packet(data):
    """Return a dict from a 'G27B1 ...' packet, or None if it isn't one."""
    try:
        parts = data.decode("ascii").split()
        if len(parts) != 8 or parts[0] != "G27B1":
            return None
        force, steer, vel, rpm, maxrpm, speed = map(float, parts[1:7])
        return {"force": force, "steer": steer, "steer_vel": vel, "rpm": rpm,
                "max_rpm": maxrpm, "speed": speed, "rack_beams": int(parts[7])}
    except (UnicodeDecodeError, ValueError):
        return None


class ForceProcessor:
    """Turns raw rack force into a -1..1 wheel command."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.smoothed = 0.0
        self.clipped = False

    def reset(self):
        self.smoothed = 0.0

    def process(self, pkt):
        c = self.cfg
        # BeamNG's rack force sign is opposite to the G27's torque direction,
        # so negate by default; "invert" flips it back for odd vehicles.
        f = -pkt["force"] / max(c["full_scale"], 1e-6) * c["strength"]
        if c["invert"]:
            f = -f
        a = min(max(c["smoothing"], 0.0), 0.95)
        self.smoothed += (1.0 - a) * (f - self.smoothed)
        out = self.smoothed - c["damping"] * pkt["steer_vel"]
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


def run(cfg, wheel, cfg_path, max_seconds=None):
    proc = ForceProcessor(cfg)
    keys = Keys()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", int(cfg["port"])))
    sock.settimeout(0.01)

    cfg["range"] = wheel.set_range(cfg["range"])
    wheel.set_autocenter(cfg["idle_autocenter"])

    active = False
    spring_on = True
    last_rx = 0.0
    last_status = 0.0
    last_led = 0.0
    peak = 0.0
    out = 0.0
    pkt = None
    note = ""
    started = time.monotonic()

    print(f"Listening for BeamNG on 127.0.0.1:{cfg['port']}. Start driving.")
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

            # --- network: drain the queue, keep only the newest packet ---
            newest = None
            try:
                while True:
                    data, _ = sock.recvfrom(512)
                    p = parse_packet(data)
                    if p:
                        newest = p
                    sock.settimeout(0)
            except (socket.timeout, BlockingIOError):
                pass
            except ConnectionResetError:  # Windows reports ICMP errors on UDP sockets
                pass
            sock.settimeout(0.01)

            if newest:
                pkt = newest
                last_rx = now
                if not active:
                    active = True
                    proc.reset()
                # Vehicles with no readable steering rack keep the spring.
                want_spring = pkt["rack_beams"] == 0
                if want_spring != spring_on:
                    wheel.set_autocenter(cfg["idle_autocenter"] if want_spring else 0)
                    spring_on = want_spring
                peak = max(peak, abs(pkt["force"]))
                if pkt["rack_beams"] > 0:
                    out = proc.process(pkt)
                    wheel.set_force(out)
                else:
                    out = 0.0
                    wheel.set_force(0)
                if cfg["leds"] and pkt["max_rpm"] > 0 and now - last_led > 0.03:
                    wheel.set_leds(rpm_leds(pkt["rpm"] / pkt["max_rpm"]))
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
                if active and pkt:
                    src = "rack" if pkt["rack_beams"] else "NO RACK (spring only)"
                    line = (f"raw {pkt['force']:8.0f}  peak {peak:7.0f}  out {out * 100:5.0f}%"
                            f"{' CLIP' if proc.clipped else '     '}  str {cfg['strength']:.1f}"
                            f"  damp {cfg['damping']:.2f}  range {cfg['range']}"
                            f"  inv {'on ' if cfg['invert'] else 'off'}"
                            f"  leds {'on ' if cfg['leds'] else 'off'}  {src}")
                else:
                    line = (f"waiting for game...  str {cfg['strength']:.1f}  damp "
                            f"{cfg['damping']:.2f}  range {cfg['range']}  inv "
                            f"{'on ' if cfg['invert'] else 'off'}  leds "
                            f"{'on ' if cfg['leds'] else 'off'}")
                if note:
                    line += f"  [{note}]"
                    note = ""
                sys.stdout.write("\r" + line.ljust(118))
                sys.stdout.flush()
    finally:
        sock.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="BeamNG.drive force feedback for the Logitech G27.")
    p.add_argument("--dry-run", action="store_true", help="no wheel; just show values")
    p.add_argument("--config", default=CONFIG_FILE)
    p.add_argument("--range", type=int, help="override steering range for this session")
    p.add_argument("--force", action="store_true", help="skip G27 revision check")
    p.add_argument("--seconds", type=float, help=argparse.SUPPRESS)  # for testing
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if args.range:
        cfg["range"] = args.range

    try:
        wheel = FakeWheel() if args.dry_run else G27.open(force=args.force)
    except G27Error as e:
        sys.exit(f"Error: {e}")

    try:
        run(cfg, wheel, args.config, max_seconds=args.seconds)
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


if __name__ == "__main__":
    main()
