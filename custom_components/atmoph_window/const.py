"""Constants for the Atmoph Window integration."""

from typing import Final

DOMAIN: Final = "atmoph_window"
DEFAULT_UPDATE_INTERVAL: Final = 60

CONF_ADVERTISED_NAME: Final = "advertised_name"
CONF_DEVICE_UUID: Final = "device_uuid"

ATTR_LOCATION: Final = "location"
ATTR_IMAGE_URL: Final = "image_url"
ATTR_PANORAMA_ROLE: Final = "panorama_role"
ATTR_REVISION: Final = "revision"

SERVICE_SEND_COMMAND: Final = "send_command"
SERVICE_SET_SETTING: Final = "set_setting"

ATTR_COMMAND: Final = "command"
ATTR_SETTING: Final = "setting"
ATTR_VALUE: Final = "value"
