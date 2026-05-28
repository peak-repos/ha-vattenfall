"""Constants for the Vattenfall supplier (Mina sidor) integration."""

from datetime import timedelta

from homeassistant.const import CONF_PASSWORD as HA_CONF_PASSWORD

DOMAIN = "vattenfall"

CONF_CUSTOMER_ID = "customer_id"
CONF_PASSWORD = HA_CONF_PASSWORD
CONF_PREMISE_ID = "premise_id"

DEFAULT_NAME = "Vattenfall"
DEFAULT_SCAN_INTERVAL = timedelta(hours=1)

ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_POINTS = "points"
ATTR_HOURLY_START_DATE = "hourly_start_date"
ATTR_HOURLY_END_DATE = "hourly_end_date"
ATTR_HOURLY_POINTS = "hourly_points"

SERVICE_BACKFILL = "backfill"
SERVICE_ATTR_START_DATE = "start_date"
SERVICE_ATTR_END_DATE = "end_date"
SERVICE_ATTR_MODE = "mode"
SERVICE_ATTR_ENTRY_ID = "entry_id"

BACKFILL_MODES = ("daily", "hourly", "all")
