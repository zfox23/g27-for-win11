"""Game adapters.

To add a game, create g27bridge/apps/<name>/ with an adapter.py that
subclasses g27bridge.core.bridge.Adapter, then register it below. It then runs
with:  python -m g27bridge <name>
"""
import importlib

# command name -> (module, adapter class name)
APPS = {
    "beamng": ("g27bridge.apps.beamng.adapter", "BeamNGAdapter"),
}


def load(name):
    module, cls = APPS[name]
    return getattr(importlib.import_module(module), cls)
