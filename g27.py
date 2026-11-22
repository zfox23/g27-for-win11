"""Logitech G27 control over raw HID -- no Logitech software or kernel driver.

Windows' built-in HID driver handles the wheel's inputs (steering, pedals,
shifter, buttons). This module only sends the wheel's vendor commands:
native-mode switch, rotation range, autocenter spring, constant force, LEDs.

The command bytes come from the Linux hid-lg4ff driver, which has supported
the G27 for over a decade.

Usage:
    python g27.py info
    python g27.py native            # switch to native mode (900 deg, clutch, shifter)
    python g27.py range 540         # set rotation lock-to-lock, 40-900
    python g27.py autocenter 30     # centering spring strength in %, 0 = off
    python g27.py test-ffb          # short left/right push to confirm FFB works
"""
import argparse
import sys
import time

VID = 0x046D
PID_NATIVE = 0xC29B  # G27 after the native-mode switch
PID_COMPAT = 0xC294  # what the G27 shows up as at power-up (Driving Force EX mode)

# bcdDevice check: identifies a G27 while it is in compat mode. hid-lg4ff uses
# mask 0xFFF8 (0x1230-0x1237), but real G27s also ship with later firmware such
# as 0x1238, so accept the whole 0x123x range. (G25s report 0x12xx below that.)
G27_REV_MASK, G27_REV_VALUE = 0xFFF0, 0x1230

NATIVE_SWITCH = (
    (0xF8, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00),
    (0xF8, 0x09, 0x04, 0x01, 0x00, 0x00, 0x00),
)


class G27Error(Exception):
    pass


def _hid():
    try:
        import hid  # provided by the "hidapi" package
    except ImportError:
        raise G27Error("Missing dependency. Run:  pip install hidapi")
    if not hasattr(hid, "device"):
        raise G27Error(
            "The installed 'hid' module is not cython-hidapi. Run:\n"
            "  pip uninstall hid\n  pip install hidapi")
    return hid


def find(pid):
    return list(_hid().enumerate(VID, pid))


def _open_path(path):
    dev = _hid().device()
    dev.open_path(path)
    return dev


def _write(dev, cmd):
    data = list(cmd) + [0] * (7 - len(cmd))
    # The G27 uses no report IDs, so hidapi needs a leading 0x00.
    if dev.write([0x00] + data) < 0:
        raise G27Error("HID write failed. Close Logitech software or other "
                       "wheel tools that might hold the device, then retry.")


def switch_to_native(timeout=8.0, force=False, log=print):
    """Switch a compat-mode G27 to native mode. Returns once it re-enumerates."""
    if find(PID_NATIVE):
        return
    compat = find(PID_COMPAT)
    if not compat:
        raise G27Error(
            "No G27 found. Check that it's plugged into USB and the power "
            "adapter is connected (the wheel won't do its startup turn without it).")
    rev = compat[0].get("release_number", 0)
    if (rev & G27_REV_MASK) != G27_REV_VALUE and not force:
        raise G27Error(
            f"Found a Logitech wheel in compatibility mode, but its revision "
            f"(0x{rev:04x}) doesn't look like a G27. Use --force to try anyway.")
    log("Switching G27 to native mode...")
    dev = _open_path(compat[0]["path"])
    try:
        for cmd in NATIVE_SWITCH:
            _write(dev, cmd)
            time.sleep(0.02)
    finally:
        try:
            dev.close()
        except Exception:
            pass
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.25)
        if find(PID_NATIVE):
            time.sleep(0.75)  # let Windows finish binding its HID driver
            log("G27 is in native mode.")
            return
    raise G27Error("Sent the native-mode switch, but the wheel didn't come back "
                   "as a G27. Unplug and replug it, then try again.")


class G27:
    """An open G27 in native mode."""

    def __init__(self, dev):
        self.dev = dev
        self._last_force_byte = None
        self._last_leds = None

    @classmethod
    def open(cls, force=False, log=print):
        switch_to_native(force=force, log=log)
        devs = find(PID_NATIVE)
        if not devs:
            raise G27Error("G27 disappeared after the native-mode switch.")
        return cls(_open_path(devs[0]["path"]))

    def send(self, *cmd):
        _write(self.dev, cmd)

    def set_range(self, degrees):
        deg = max(40, min(900, int(degrees)))
        self.send(0xF8, 0x81, deg & 0xFF, (deg >> 8) & 0xFF)
        return deg

    def set_autocenter(self, fraction):
        """Centering spring, 0.0 (off) to 1.0 (strongest)."""
        fraction = max(0.0, min(1.0, float(fraction)))
        if fraction == 0:
            self.send(0xF5)
            return
        mag = int(fraction * 0xFFFF)
        if mag <= 0xAAAA:
            a, b = 0x0C * mag, 0x80 * mag
        else:
            a = 0x0C * 0xAAAA + 0x06 * (mag - 0xAAAA)
            b = 0x80 * 0xAAAA + 0xFF * (mag - 0xAAAA)
        a >>= 1  # non-MOMO wheels use half the expansion slope
        self.send(0xFE, 0x0D, a // 0xAAAA, a // 0xAAAA, b // 0xAAAA)
        self.send(0x14)

    def set_force(self, level):
        """Constant force, -1.0 to 1.0. Repeated values are not re-sent."""
        level = max(-1.0, min(1.0, float(level)))
        x = 0x80 + int(round(level * 127))
        if x == self._last_force_byte:
            return
        self._last_force_byte = x
        if x == 0x80:
            self.send(0x13)
        else:
            self.send(0x11, 0x08, x, 0x80)

    def stop_force(self):
        self._last_force_byte = 0x80
        self.send(0x13)

    def set_leds(self, mask):
        mask &= 0x1F
        if mask != self._last_leds:
            self._last_leds = mask
            self.send(0xF8, 0x12, mask)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass


def rpm_leds(fraction):
    """Map 0..1 of redline to the G27's 5 rev LEDs (bit mask)."""
    if fraction >= 0.97:
        return 0x1F if int(time.monotonic() * 8) % 2 else 0x00  # flash at redline
    lit = 0
    for i, threshold in enumerate((0.60, 0.70, 0.80, 0.88, 0.94)):
        if fraction >= threshold:
            lit |= 1 << i
    return lit


def _cmd_info(_args):
    for pid, label in ((PID_NATIVE, "native mode"), (PID_COMPAT, "compatibility mode")):
        for d in find(pid):
            print(f"{label}: {d.get('product_string')!r}  "
                  f"rev=0x{d.get('release_number', 0):04x}  path={d['path']!r}")
    if not find(PID_NATIVE) and not find(PID_COMPAT):
        print("No G27 found.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Control a Logitech G27 without Logitech drivers.")
    p.add_argument("--force", action="store_true",
                   help="skip the G27 revision check when switching modes")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="list connected G27s")
    sub.add_parser("native", help="switch to native mode")
    r = sub.add_parser("range", help="set rotation range (degrees lock-to-lock)")
    r.add_argument("degrees", type=int)
    a = sub.add_parser("autocenter", help="set centering spring (0-100%%)")
    a.add_argument("percent", type=float)
    sub.add_parser("test-ffb", help="push the wheel briefly each way")
    args = p.parse_args(argv)

    try:
        if args.cmd == "info":
            _cmd_info(args)
            return
        if args.cmd == "native":
            switch_to_native(force=args.force)
            return
        wheel = G27.open(force=args.force)
        try:
            if args.cmd == "range":
                print(f"Range set to {wheel.set_range(args.degrees)} degrees.")
            elif args.cmd == "autocenter":
                wheel.set_autocenter(args.percent / 100)
                print(f"Autocenter set to {args.percent:.0f}%.")
            elif args.cmd == "test-ffb":
                wheel.set_autocenter(0)
                for label, level in (("negative", -0.35), ("positive", 0.35)):
                    print(f"{label} force for 1 s...")
                    wheel.set_force(level)
                    time.sleep(1.0)
                wheel.stop_force()
                print("Done. If the wheel moved both ways, force feedback works.")
        finally:
            wheel.close()
    except G27Error as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
