"""Constants for the HPE Aruba Networking Instant On integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "instant_on"
MANUFACTURER: Final = "HPE Aruba Networking"
PORTAL_URL: Final = "https://portal.instant-on.hpe.com"

CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_OTP: Final = "otp"
CONF_SITES: Final = "sites"
CONF_TRACK_CLIENTS: Final = "track_clients"
CONF_PORT_SENSORS: Final = "port_sensors"
CONF_SCAN_INTERVAL: Final = "scan_interval"

TRACK_ALL: Final = "all"
TRACK_WATCHLIST: Final = "watchlist"
TRACK_NONE: Final = "none"

DEFAULT_SCAN_INTERVAL: Final = 120
MIN_SCAN_INTERVAL: Final = 60
SLOW_UPDATE_INTERVAL: Final = timedelta(minutes=10)
EVENTS_INTERVAL: Final = timedelta(minutes=5)
# Backoff after rate limiting or server errors: doubles per failure, capped.
MAX_BACKOFF: Final = timedelta(minutes=15)
# Random extra delay (fraction of the interval) so installs don't poll in lockstep.
JITTER: Final = 0.1

# Fetched every scan interval, per site.
FAST_ENDPOINTS: Final = ["inventory", "clientSummary"]
# Fetched at most every EVENTS_INTERVAL, per site.
EVENT_ENDPOINTS: Final = ["events"]
# Fetched every SLOW_UPDATE_INTERVAL, per site.
SLOW_ENDPOINTS: Final = ["landingPage", "maintenance", "networksSummary", "wiredNetworks"]

EVENT_INSTANT_ON: Final = f"{DOMAIN}_event"
EVENT_INSTANT_ON_ALERT: Final = f"{DOMAIN}_alert"

SERVICE_LOOKUP_MAC: Final = "lookup_mac"
SIGNAL_ENTRIES_CHANGED: Final = f"{DOMAIN}_entries_changed"
ATTR_MAC: Final = "mac"
