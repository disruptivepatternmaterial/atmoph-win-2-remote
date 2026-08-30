"""Tests for the Home Assistant layer of the Atmoph Window integration.

`tests/conftest.py` puts stand-in package objects in `sys.modules` so the
protocol suite can import `client` and `protocol` without executing the
integration's `__init__.py`. These tests need the opposite: the real package,
Home Assistant imports and all, which also means `homeassistant` ends up in
`sys.modules` and `test_protocol_layer_is_home_assistant_free` would fail. The
two suites therefore cannot share a process, so `norecursedirs` keeps this
directory out of the default run and CI gives it a job of its own.
"""
