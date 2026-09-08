"""Tests for the diagnostics in tools/.

`tools/atmoph_diag.py` imports the standard library and
`custom_components/atmoph_window/protocol.py`, and `tools/atmoph_netscan.py`
imports the standard library alone, so both belong in the fast suite rather
than beside the Home Assistant tests. Nothing under this directory may import
Home Assistant: `test_protocol_layer_is_home_assistant_free` shares the
process and asserts it never arrived.
"""
