"""BeamNG.drive adapter.

The g27bridge mod (mod/ in this folder, packed as g27bridge.zip) computes the
player vehicle's steering force the way BeamNG's own FFB does and sends it
over UDP to 127.0.0.1:<port>.

Packet (text): "G27B2 <ffb> <steer> <steerVel> <rpm> <maxRpm> <speed> <hydros>"
    ffb: 1.0 = BeamNG's full FFB torque at its default strength; positive
         pushes the wheel towards negative steering (left).
"""
import os
import socket

from ...core.bridge import Adapter, Frame

LOWSPEED_FULL = 5 / 3.6   # m/s; BeamNG ramps FFB from 10% at rest to 100% here
LOWSPEED_MIN = 0.1


def parse_packet(data):
    """Return a dict from a 'G27B1'/'G27B2' packet, or None if it isn't one."""
    try:
        parts = data.decode("ascii").split()
        if len(parts) != 8 or parts[0] not in ("G27B1", "G27B2"):
            return None
        force, steer, vel, rpm, maxrpm, speed = map(float, parts[1:7])
        return {"version": parts[0], "force": force, "steer": steer, "steer_vel": vel,
                "rpm": rpm, "max_rpm": maxrpm, "speed": speed, "rack": int(parts[7])}
    except (UnicodeDecodeError, ValueError):
        return None


def lowspeed_coef(speed):
    """BeamNG's default 'lowspeedCoef': FFB is 10% when parked, rising
    linearly to full at 5 km/h, so tyre scrub doesn't fight you in car parks."""
    k = min(max(abs(speed) / LOWSPEED_FULL, 0.0), 1.0)
    return LOWSPEED_MIN + (1.0 - LOWSPEED_MIN) * k


class BeamNGAdapter(Adapter):
    name = "beamng"
    title = "BeamNG"
    defaults = {
        "port": 4445,
    }
    # Settings saved before the repo split into apps; read if config/beamng.json is absent.
    legacy_config = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")

    def __init__(self, cfg):
        super().__init__(cfg)
        # 'full_scale' calibrated the old mod's raw beam force; the force is now
        # already normalised, and 'strength' is the only gain.
        cfg.pop("full_scale", None)
        self.sock = None
        self.pkt = None
        self.peak = 0.0

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", int(self.cfg["port"])))
        self.sock.setblocking(False)
        return f"127.0.0.1:{self.cfg['port']}"

    def poll(self):
        # Drain whatever has arrived and keep the newest packet. Never blocks.
        newest = None
        try:
            while True:
                data, _ = self.sock.recvfrom(512)
                p = parse_packet(data)
                if p:
                    newest = p
        except (socket.timeout, BlockingIOError):
            pass
        except ConnectionResetError:  # Windows reports ICMP errors on UDP sockets
            pass
        if newest is None:
            return None

        self.pkt = newest
        speed = abs(newest["speed"])
        rpm_frac = newest["rpm"] / newest["max_rpm"] if newest["max_rpm"] > 0 else None
        # The old mod (G27B1) read the wrong beam; its force is noise. Hold the
        # spring until the mod is updated.
        if newest["version"] != "G27B2" or newest["rack"] == 0:
            return Frame(force=None, steer_vel=newest["steer_vel"], rpm_frac=rpm_frac,
                         speed=speed)
        self.peak = max(self.peak, abs(newest["force"]))
        # Mod: + pushes towards left. Frame: + turns the wheel right.
        force = -newest["force"] * lowspeed_coef(speed)
        return Frame(force=force, steer_vel=newest["steer_vel"], rpm_frac=rpm_frac,
                     speed=speed)

    def status(self, frame):
        p = self.pkt
        if p["version"] != "G27B2":
            return "OLD MOD: replace g27bridge.zip in BeamNG's mods folder (spring only)"
        if not p["rack"]:
            return "NO RACK (spring only)"
        return f"ffb {p['force'] * 100:5.0f}%  peak {self.peak * 100:4.0f}%"

    def close(self):
        if self.sock:
            self.sock.close()
