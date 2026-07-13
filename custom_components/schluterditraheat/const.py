"""Constants for the Schluter DITRA-HEAT integration."""
from datetime import timedelta

DOMAIN = "schluterditraheat"

# Configuration
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# API
API_BASE_URL = "https://schluterditraheat.com/api"
API_TIMEOUT = 30

# Update interval. Sinope (the RS1's backend OEM) asks integrators to poll no
# faster than 300s; polling much faster risks the frequency-based session/login
# limits. Do not exceed ~600s or the server session expires (USRSESSEXP).
SCAN_INTERVAL = timedelta(seconds=300)

# Energy statistics refresh interval (matches the cloud's hourly consumption buckets)
ENERGY_UPDATE_INTERVAL = timedelta(hours=1)

# Static data cache refresh (polls between full refreshes; ~1 hour at 300s interval)
STATIC_REFRESH_INTERVAL_POLLS = 12

# Rate limit backoff
RATE_LIMIT_INITIAL_BACKOFF = timedelta(minutes=2)
RATE_LIMIT_MAX_BACKOFF = timedelta(minutes=16)
RATE_LIMIT_BACKOFF_FACTOR = 2

# Maximum pause after a daily-cap (ACCDAYREQMAX) hit. The pause targets the next
# local midnight but is capped at this value so polling re-checks periodically —
# the backend's true reset boundary (UTC vs. local) is not certain.
DAILY_LIMIT_MAX_PAUSE = timedelta(hours=1)

# Proactive throttle: when the API's reported remaining budget drops to or below
# this floor, defer the next poll until the rate-limit window resets.
#
# Observed limits (authenticated session, 2026-07): the polling routes allow
# 120 requests per rolling 10-second window (x-ratelimit-reset counts down in
# seconds). That is far more headroom than the default 300s poll needs, so this
# floor is a safety net for bursts (many thermostats, retries) rather than a
# constraint hit in normal operation. Login is capped far tighter (limit 3).
RATE_LIMIT_REMAINING_FLOOR = 1

# Temperature limits (Celsius)
MIN_TEMP_C = 5.0
MAX_TEMP_C = 32.0

# Attributes
ATTR_DEVICE_ID = "device_id"
ATTR_IDENTIFIER = "identifier"
ATTR_GROUP_NAME = "group_name"
ATTR_LOCATION_NAME = "location_name"

# Modes
MODE_AUTO = "auto"
MODE_OFF = "off"
MODE_MANUAL = "autoBypass"  # For manual temperature override
