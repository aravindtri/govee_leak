"""Constants for the GoveeLife Water Leak integration."""
from __future__ import annotations

DOMAIN = "govee_leak"

CONF_CODE = "code"

# Default %: at or below this the "Low battery" binary sensor turns on.
DEFAULT_LOW_BATTERY_PCT = 20

# Dispatcher signals (suffixed with entry_id / device id at send time).
SIGNAL_UPDATE = "govee_leak_update"
SIGNAL_AVAILABILITY = "govee_leak_availability"

LEAK_SKU = "H5059"
GATEWAY_SKUS = {"H5044", "H5043"}
