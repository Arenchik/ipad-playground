"""Constants for the TWRA Aroma Diffuser integration."""
from datetime import timedelta

DOMAIN = "twra_diffuser"

CONF_KEEP_CONNECTED = "keep_connected"
DEFAULT_KEEP_CONNECTED = True
IDLE_TIMEOUT = 30.0

REFRESH_INTERVAL = timedelta(minutes=5)
TIME_SYNC_INTERVAL = timedelta(hours=6)

# The intensity slider spans these pauses between sprays (1 % -> MAX).
MIN_PAUSE = 30
MAX_PAUSE = 900

# Spray durations offered as buttons; 16 s matches the tested unit's mode.
SPRAY_TIMES: tuple[int, ...] = (5, 10, 16, 20, 30, 45, 60)

# Named scent modes for the HomeKit pop-up list: key, spray s, pause s.
SCENT_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("faint", 5, 900),
    ("light", 10, 600),
    ("standard", 16, 480),
    ("rich", 20, 300),
    ("strong", 30, 180),
    ("intense", 45, 90),
    ("max", 60, 30),
)
PRESET_CUSTOM = "custom"

SERVICE_SET_WORKING_MODE = "set_working_mode"
ATTR_DEVICE_ID = "device_id"
ATTR_MODE = "mode"
ATTR_ENABLED = "enabled"
ATTR_DAYS = "days"
ATTR_START = "start"
ATTR_END = "end"
ATTR_RUN_SECONDS = "run_seconds"
ATTR_PAUSE_SECONDS = "pause_seconds"
