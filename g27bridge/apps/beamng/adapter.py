"""BeamNG.drive adapter.

The g27bridge mod (mod/ in this folder, packed as g27bridge.zip) sends the
steering-rack force of the player's vehicle over UDP to 127.0.0.1:<port>.

Packet (text): "G27B1 <rackForce> <steer> <steerVel> <rpm> <maxRpm> <speed> <rackBeams>"
"""
import os
import socket

from ...core.bridge import Adapter, Frame


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


class BeamNGAdapter(Adapter):
    name = "beamng"
    title = "BeamNG"
    defaults = {
        "port": 4445,
        "full_scale": 2000.0,  # rack force that equals 100% wheel torque at strength 1
    }
    # Settings saved before the repo split into apps; read if config/beamng.json is absent.
    legacy_config = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")

    def __init__(self, cfg):
        super().__init__(cfg)
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
        self.peak = max(self.peak, abs(newest["force"]))
        rpm_frac = newest["rpm"] / newest["max_rpm"] if newest["max_rpm"] > 0 else None
        if newest["rack_beams"] == 0:  # vehicle has no readable steering rack
            return Frame(force=None, steer_vel=newest["steer_vel"], rpm_frac=rpm_frac,
                         speed=abs(newest["speed"]))
        # BeamNG's rack force sign is opposite to the G27's torque direction.
        force = -newest["force"] / max(self.cfg["full_scale"], 1e-6)
        return Frame(force=force, steer_vel=newest["steer_vel"], rpm_frac=rpm_frac,
                     speed=abs(newest["speed"]))

    def status(self, frame):
        p = self.pkt
        src = "rack" if p["rack_beams"] else "NO RACK (spring only)"
        return f"raw {p['force']:8.0f}  peak {self.peak:7.0f}  {src}"

    def close(self):
        if self.sock:
            self.sock.close()
