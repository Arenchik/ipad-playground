"""Constants for the TWRA Aroma Diffuser integration."""
from datetime import timedelta

DOMAIN = "twra_diffuser"

CONF_KEEP_CONNECTED = "keep_connected"
DEFAULT_KEEP_CONNECTED = True
IDLE_TIMEOUT = 30.0

REFRESH_INTERVAL = timedelta(minutes=5)
# Reconnect back-off after a lost link, in seconds. Hearing the diffuser
# advertise cuts the wait short.
RECONNECT_MIN = 5.0
RECONNECT_MAX = 60.0
TIME_SYNC_INTERVAL = timedelta(hours=6)

# The intensity slider spans these pauses between sprays (1 % -> MAX).
MIN_PAUSE = 30
MAX_PAUSE = 900

# Spray durations offered as buttons; 16 s matches the tested unit's mode.
SPRAY_TIMES: tuple[int, ...] = (5, 10, 16, 20, 30, 45, 60)

# Scent modes offered as buttons: key, spray s, pause s. Short bursts with
# long pauses: the nose stops noticing a constant scent, so spacing the
# sprays keeps it perceptible while using less oil. Ordered weakest first.
SCENT_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("minimum", 8, 900),
    ("eco", 10, 600),
    ("balanced", 12, 480),
    ("rich", 16, 300),
)

# "Guests": a strong burst that hands the previous settings back after a while.
BOOST_RUN = 30
BOOST_PAUSE = 120
BOOST_DURATION = timedelta(minutes=20)

SERVICE_SET_SPRAY_CYCLE = "set_spray_cycle"
ATTR_DEVICE_ID = "device_id"
ATTR_RUN_SECONDS = "run_seconds"
ATTR_PAUSE_SECONDS = "pause_seconds"
