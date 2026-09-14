"""Consistency between the integration's declared metadata and its code.

None of this boots Home Assistant: it reads the JSON and YAML that ship in the
integration folder and compares them with the protocol tables. It lives in the
Home Assistant free suite so it runs in both CI jobs and finishes in
milliseconds, because hassfest only runs in CI and a missing string is much
cheaper to find before a push than after one.
"""

from __future__ import annotations

import json
import pathlib

import yaml

from custom_components.atmoph_window.const import DOMAIN
from custom_components.atmoph_window.protocol import COMMANDS, SETTING_KEYS

INTEGRATION = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / DOMAIN
)


def load_services() -> dict[str, dict]:
    """Return `services.yaml`, read without Home Assistant's loader."""
    return yaml.safe_load((INTEGRATION / "services.yaml").read_text())


def load_json(name: str) -> dict:
    """Return one of the integration's JSON documents."""
    return json.loads((INTEGRATION / name).read_text())


def test_english_translations_match_the_source_strings() -> None:
    """`translations/en.json` is the shipped copy of `strings.json`."""
    assert load_json("strings.json") == load_json("translations/en.json")


def test_every_service_and_field_is_documented() -> None:
    """hassfest rejects a service or field with no name, and so does this."""
    services = load_services()
    strings = load_json("strings.json")

    assert set(services) == set(strings["services"])
    for name, schema in services.items():
        documented = strings["services"][name]
        assert documented["name"]
        assert documented["description"]
        assert set(schema["fields"]) == set(documented["fields"])
        for field in documented["fields"].values():
            assert field["name"]
            assert field["description"]


def test_service_pickers_offer_exactly_the_protocol_tokens() -> None:
    """A picker that drifts from the protocol offers a token the handler refuses."""
    services = load_services()

    def options(service: str, field: str) -> set[str]:
        return set(services[service]["fields"][field]["selector"]["select"]["options"])

    assert options("send_command", "command") == set(COMMANDS)
    assert options("set_setting", "setting") == set(SETTING_KEYS)


def test_service_targets_carry_no_device_filter() -> None:
    """hassfest refuses a device filter on a service target."""
    for schema in load_services().values():
        assert "device" not in schema["target"]
        assert schema["target"]["entity"] == {"integration": DOMAIN}


def test_declared_icons_belong_to_declared_entities() -> None:
    """An icon under an unknown translation key is silently never shown."""
    icons = load_json("icons.json")
    strings = load_json("strings.json")

    for platform, entries in icons["entity"].items():
        assert set(entries) <= set(strings["entity"][platform])
        for entry in entries.values():
            assert entry["default"].startswith("mdi:")


def test_every_error_the_code_raises_has_a_message() -> None:
    """A translation key with no entry renders as the key itself to the user.

    These are the messages someone sees when a window will not answer, so an
    untranslated one lands in front of exactly the person least able to work
    out what it meant.
    """
    declared = set(load_json("strings.json")["exceptions"])
    raised = {
        "duplicate_device_uuid",
        "no_window_targeted",
        "not_reachable",
        "power_not_confirmed",
        "setting_not_reported",
        "unknown_command",
        "unknown_setting",
        "value_out_of_range",
        "wrong_window",
    }

    assert raised == declared
