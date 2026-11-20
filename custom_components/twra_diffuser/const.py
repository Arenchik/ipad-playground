"""Constants for the TWRA Aroma Diffuser integration."""
from datetime import timedelta

DOMAIN = "twra_diffuser"

CONF_KEEP_CONNECTED = "keep_connected"
DEFAULT_KEEP_CONNECTED = True
IDLE_TIMEOUT = 30.0

REFRESH_INTERVAL = timedelta(minutes=5)
TIME_SYNC_INTERVAL = timedelta(hours=6)

# Fan speed steps map to the pause between sprays of the enabled working
# modes, slowest (weakest scent) first. The run time is left alone.
INTENSITY_PAUSES: tuple[int, ...] = (900, 600, 450, 300, 180, 90)

SERVICE_SET_WORKING_MODE = "set_working_mode"
ATTR_DEVICE_ID = "device_id"
ATTR_MODE = "mode"
ATTR_ENABLED = "enabled"
ATTR_DAYS = "days"
ATTR_START = "start"
ATTR_END = "end"
ATTR_RUN_SECONDS = "run_seconds"
ATTR_PAUSE_SECONDS = "pause_seconds"
