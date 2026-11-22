"""Command line entry point.

    python -m g27bridge beamng [--dry-run] [--range N] ...   run the bridge for a game
    python -m g27bridge wheel info|native|range|autocenter|test-ffb
"""
import argparse
import sys

from . import apps
from .core import bridge, g27

USAGE = """usage: python -m g27bridge <command> [options]

commands:
  wheel ...   control the wheel directly (info, native, range, autocenter, test-ffb)
{apps}
Run 'python -m g27bridge <command> -h' for that command's options."""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    app_lines = "".join(f"  {n:<11} force-feedback bridge for {apps.load(n).title}\n"
                        for n in apps.APPS)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE.format(apps=app_lines))
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "wheel":
        g27.main(rest, prog="python -m g27bridge wheel")
        return
    if cmd not in apps.APPS:
        sys.exit(f"Unknown command {cmd!r}.\n\n" + USAGE.format(apps=app_lines))
    adapter_cls = apps.load(cmd)
    p = argparse.ArgumentParser(prog=f"python -m g27bridge {cmd}",
                                description=f"{adapter_cls.title} force feedback for the G27.")
    bridge.add_bridge_args(p)
    bridge.main(adapter_cls, p.parse_args(rest),
                legacy_config=getattr(adapter_cls, "legacy_config", None))


if __name__ == "__main__":
    main()
