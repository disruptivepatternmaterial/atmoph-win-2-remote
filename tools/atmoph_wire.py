"""Reach the integration's wire protocol from a host without Home Assistant.

`custom_components/atmoph_window/__init__.py` is the Home Assistant entry
point, so importing the package normally pulls in Home Assistant. The wire
protocol imports none of it, and the diagnostics have to run on a laptop with
nothing but bleak installed, so this module registers the package objects in
`sys.modules` directly and loads `protocol` against them.
`tests/conftest.py` does the same thing for the same reason.

The registration has to happen before the first import of `protocol`, so every
other module in tools/ takes its protocol names from here rather than
importing `protocol` itself. That is the whole reason this module exists: it
makes the ordering a property of the import graph rather than a side effect of
whichever module happened to be loaded first.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _register_namespace(name: str, path: Path) -> None:
    """Put a package in sys.modules without executing its __init__.py."""
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_register_namespace("custom_components", _REPO_ROOT / "custom_components")
_register_namespace(
    "custom_components.atmoph_window",
    _REPO_ROOT / "custom_components" / "atmoph_window",
)

from custom_components.atmoph_window.protocol import (  # noqa: E402
    COMMAND_UUID,
    FOCUSING_VIEW_UUID,
    IDENTITY_UUID,
    PANORAMA_ROLE_UUID,
    POWER_UUID,
    QUICK_SETTINGS_UUID,
    SERVICE_UUID,
    SETTING_KEYS,
    TEXT_INPUT_UUID,
    VIEW_ID_UUID,
    VIEW_IMAGE_UUID,
    VIEW_LOCATION_UUID,
    VIEW_TITLE_UUID,
    JsonObjectStream,
    Level,
    decode_text,
    encode_command,
    encode_setting,
)

__all__ = [
    "COMMAND_UUID",
    "FOCUSING_VIEW_UUID",
    "IDENTITY_UUID",
    "PANORAMA_ROLE_UUID",
    "POWER_UUID",
    "QUICK_SETTINGS_UUID",
    "SERVICE_UUID",
    "SETTING_KEYS",
    "TEXT_INPUT_UUID",
    "VIEW_ID_UUID",
    "VIEW_IMAGE_UUID",
    "VIEW_LOCATION_UUID",
    "VIEW_TITLE_UUID",
    "JsonObjectStream",
    "Level",
    "decode_text",
    "encode_command",
    "encode_setting",
]
