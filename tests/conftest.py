"""Load the transport modules without pulling in Home Assistant.

`custom_components/atmoph_window/__init__.py` is the Home Assistant entry
point, so importing the package normally pulls in
`homeassistant.components.bluetooth` and, transitively, Home Assistant's USB
and serial stacks. `protocol.py` and `client.py` deliberately import none of
that, so these tests register the package objects directly and let the
submodules load against them.

The indirection is also a guard: if a Home Assistant import ever leaks into
the protocol layer, `test_protocol_layer_is_home_assistant_free` fails.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = ROOT / "custom_components" / "atmoph_window"


def _register_package(name: str, path: Path) -> None:
    """Put a package in `sys.modules` without executing its `__init__.py`."""
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


_register_package("custom_components", ROOT / "custom_components")
_register_package("custom_components.atmoph_window", PACKAGE_ROOT)
