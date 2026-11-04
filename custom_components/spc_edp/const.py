"""Constants for the Vanderbilt SPC (EDP) integration."""

from __future__ import annotations

DOMAIN = "spc_edp"
MANUFACTURER = "Vanderbilt (Siemens)"

# Config entry data
CONF_RECEIVER_ID = "receiver_id"
CONF_BIND = "bind"
CONF_PORT = "port"
CONF_ENCRYPTION_KEY = "encryption_key"

# --- Config entry option keys (tunable after setup) -------------------------
CONF_IDLE_TIMEOUT = "idle_timeout"
CONF_AREA_REFRESH_INTERVAL = "area_refresh_interval"
CONF_RECONNECT_GRACE = "reconnect_grace"
CONF_MAPPING_GATE_POLL_INTERVAL = "mapping_gate_poll_interval"

DEFAULT_BIND = "0.0.0.0"  # nosec B104 # panels initiate connections on the LAN.
DEFAULT_PORT = 50000
DEFAULT_IDLE_TIMEOUT = 120
# EDP event reporting can be selectively disabled at the panel. A 30-second
# zone refresh is the lowest interval exposed by the options flow and keeps
# physical state current even when the panel does not publish SIA ZO/ZC events.
DEFAULT_AREA_REFRESH_INTERVAL = 30  # seconds; reconciles event drift/fallback
# Panels drop and redial the EDP link several times a day and are back within
# seconds, delivering queued events after the redial. Entities keep their last
# state for this long instead of flapping through "unavailable".
DEFAULT_RECONNECT_GRACE = 60  # seconds; 0 marks entities unavailable at once
# The panel sends no event when a mapping gate changes, and a door's
# momentary unlock lasts a few seconds, so mapping gates are read this often.
DEFAULT_MAPPING_GATE_POLL_INTERVAL = 1  # seconds; 0 reads them only with the refresh

MIN_PORT = 1
MAX_PORT = 65535
MIN_RECEIVER_ID = 1
MAX_RECEIVER_ID = 999997

# --- Dispatcher signals ------------------------------------------------------
SIGNAL_AVAILABILITY = "spc_edp_availability_{}"
SIGNAL_UPDATE_AREA = "spc_edp_update_area_{}_{}"
SIGNAL_UPDATE_ZONE = "spc_edp_update_zone_{}_{}"
SIGNAL_NEW_AREAS = "spc_edp_new_areas_{}"
SIGNAL_NEW_ZONES = "spc_edp_new_zones_{}"
SIGNAL_UPDATE_DOOR = "spc_edp_update_door_{}_{}"
SIGNAL_NEW_DOORS = "spc_edp_new_doors_{}"
SIGNAL_DOOR_ACCESS = "spc_edp_door_access_{}_{}"
SIGNAL_AREA_CHANGE = "spc_edp_area_change_{}_{}"
SIGNAL_UPDATE_MAPPING_GATE = "spc_edp_update_mapping_gate_{}_{}"
SIGNAL_NEW_MAPPING_GATES = "spc_edp_new_mapping_gates_{}"
SIGNAL_UPDATE_SYSTEM = "spc_edp_update_system_{}"
SIGNAL_PANEL_EVENT = "spc_edp_panel_event_{}"

# SIA access-control codes forwarded to door event entities, keyed by their
# names in Vanderbilt's SIA code table. DG, DD, DX, DF and DR were observed on
# an SPC4300; DN, DI, RL and RG follow the table. DR restores both a forced
# door and one left open.
DOOR_ACCESS_EVENT_TYPES: dict[str, str] = {
    "DG": "access_granted",
    "DD": "access_denied",
    "DI": "access_denied_passback",
    "DX": "request_to_exit",
    "DF": "door_forced",
    "DN": "door_left_open",
    "DR": "door_restoral",
    "RL": "reader_locked",
    "RG": "reader_unlocked",
}

# Door alarm conditions raised and cleared by access events.
DOOR_ALARM_SET_CODES = frozenset({"DF", "DN"})
DOOR_ALARM_CLEAR_CODES = frozenset({"DR"})

# SIA codes that follow a door mode change: DC after setting the door to
# locked (no access at all), DO after normal or unlocked. The mode itself is
# read from DOOR_STATUS; the description names who made the change
# ("<door>¦<user>¦<user id>¦<user's card>").
DOOR_MODE_SIA_CODES = frozenset({"DC", "DO"})

# Every SIA code dispatched to a door's entities.
DOOR_SIA_CODES = frozenset(DOOR_ACCESS_EVENT_TYPES) | DOOR_MODE_SIA_CODES

# Area set/unset codes whose description may name the user. The address
# carries the area number.
AREA_CHANGE_SIA_CODES = frozenset(
    {"CG", "CL", "CP", "CQ", "CS", "NL", "OA", "OG", "OP", "OQ", "OR", "OS"}
)

# Access-denied reason codes in the last field of a DD description, as the
# panel's access log words them ("Entry Denied - Invalid pin",
# "Entry Denied - INVALID CARD FORMAT").
ACCESS_DENIED_REASONS: dict[str, str] = {
    "4": "invalid_pin",
    "7": "invalid_card_format",
}

# The panel attributes EDP commands to its remote user.
EDP_USER_ID = "9998"
# An EDP-attributed event this soon after a Home Assistant command is ours.
COMMAND_ATTRIBUTION_WINDOW = 15.0  # seconds

SOURCE_HOME_ASSISTANT = "home_assistant"
SOURCE_PANEL = "panel"
SOURCE_PANEL_AUTOMATIC = "panel_automatic"
SOURCE_REMOTE = "remote"

# Events the panel re-sends after a redial carry their original timestamp.
DUPLICATE_EVENT_MEMORY = 128


# SIA codes whose address is a zone, for naming it on the panel event entity.
ZONE_EVENT_CODES = frozenset(
    {
        "BA", "BB", "BC", "BD", "BE", "BJ", "BR", "BT", "BU", "BV", "BX",
        "FA", "FB", "FC", "FJ", "FR", "FT", "FU",
        "HA", "HB", "HJ", "HR", "HT", "HU", "HV",
        "MA", "MB", "MJ", "MR", "MT", "MU",
        "PA", "PB", "PJ", "PR", "PT", "PU",
        "TA", "TB", "TP", "TR", "TU",
        "UA", "UB", "UJ", "UR", "UT", "UU",
        "XR", "XT", "ZC", "ZD", "ZM", "ZO", "ZX",
    }
)  # fmt: skip
