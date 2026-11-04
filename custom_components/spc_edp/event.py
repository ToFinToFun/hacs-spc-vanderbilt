"""Event platform for Vanderbilt SPC (EDP) door access and every panel event."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from spcedp import SIA_CODE_NAMES, Door, SiaEvent, sia_code_name

from .attribution import event_user
from .const import (
    ACCESS_DENIED_REASONS,
    AREA_CHANGE_SIA_CODES,
    DOOR_ACCESS_EVENT_TYPES,
    DOOR_SIA_CODES,
    SIGNAL_DOOR_ACCESS,
    SIGNAL_NEW_DOORS,
    SIGNAL_PANEL_EVENT,
    ZONE_EVENT_CODES,
)
from .entity import SpcEdpEntity, door_label
from .hub import SpcEdpHub

# Access descriptions are "<door>¦<user>¦<user id>¦<card>¦<direction>[¦<reason>]";
# the card is the user's registered card, also when they used a PIN or the app.
# Denied attempts name no user and append the panel's numeric reason code.
_ACCESS_FIELDS = ("door", "user", "user_id", "card", "direction", "reason_code")


def access_attributes(event: SiaEvent) -> dict[str, str]:
    """Split an access event description into its fields, naming the deny reason."""
    values = event.description.split("¦")
    attributes = {
        key: value
        for key, value in zip(_ACCESS_FIELDS, values, strict=False)
        if value
        and not (key == "user_id" and value == "0")
        and not (key == "card" and value == "0")
    }
    if (code := attributes.get("reason_code")) is not None:
        attributes["reason"] = ACCESS_DENIED_REASONS.get(code, "unknown")
    return attributes


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub], async_add_entities: AddEntitiesCallback
) -> None:
    """Set up access event entities for known and future doors."""
    hub: SpcEdpHub = entry.runtime_data
    added: set[int] = set()

    @callback
    def _add_doors(door_ids: set[int]) -> None:
        new = door_ids - added
        if not new:
            return
        added.update(new)
        async_add_entities(SpcEdpDoorAccessEvent(hub, door_id) for door_id in new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DOORS.format(entry.entry_id), _add_doors)
    )

    async_add_entities([SpcEdpPanelEvent(hub)])
    if hub.panel is not None:
        _add_doors(set(hub.panel.doors))


# Event types for the panel event entity: each SIA code, lower case.
UNKNOWN_EVENT_TYPE = "unknown"
PANEL_EVENT_TYPES = [*sorted(code.lower() for code in SIA_CODE_NAMES), UNKNOWN_EVENT_TYPE]


class SpcEdpPanelEvent(SpcEdpEntity, EventEntity):
    """Every event the panel reports, as its SIA code.

    Attributes carry the code, its name from Vanderbilt's SIA code table, the
    address and raw description, the panel's time, the user it names and the
    zone, area or door the address refers to.
    """

    _attr_event_types = PANEL_EVENT_TYPES
    _attr_translation_key = "panel_event"

    def __init__(self, hub: SpcEdpHub) -> None:
        """Initialize the panel event entity."""
        super().__init__(hub)
        self._attr_unique_id = f"{hub.unique_id}-panel-event"

    async def async_added_to_hass(self) -> None:
        """Subscribe to every panel event."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_PANEL_EVENT.format(self._hub.entry.entry_id),
                self._handle_event,
            )
        )

    def _target(self, code: str, address: str) -> dict[str, str]:
        """Name the zone, area or door an event's address refers to."""
        panel = self._hub.panel
        if panel is None or not address.isdigit():
            return {}
        number = int(address)
        if code in ZONE_EVENT_CODES and (zone := panel.zones.get(number)):
            return {"zone": zone.name}
        if code in AREA_CHANGE_SIA_CODES and (area := panel.areas.get(number)):
            return {"area": area.name}
        if code in DOOR_SIA_CODES and (door := panel.doors.get(number)):
            return {"door": door.name}
        return {}

    @callback
    def _handle_event(self, event: SiaEvent) -> None:
        code = event.sia_code.upper()
        event_type = code.lower() if code in SIA_CODE_NAMES else UNKNOWN_EVENT_TYPE
        address = event.address.strip()
        user, user_id = event_user(event)
        attributes: dict[str, str | None] = {
            "code": code,
            "name": sia_code_name(code),
            "address": address or None,
            "description": event.description or None,
            "panel_time": (
                event.timestamp.replace(tzinfo=dt_util.get_default_time_zone()).isoformat()
                if event.timestamp
                else None
            ),
            **self._target(code, address),
        }
        if user_id is not None:
            attributes["user"] = user
            attributes["user_id"] = user_id
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()


class SpcEdpDoorAccessEvent(SpcEdpEntity, EventEntity):
    """Access granted, denied and exit requests reported for one door."""

    _attr_event_types = sorted(set(DOOR_ACCESS_EVENT_TYPES.values()))
    _attr_translation_key = "door_access"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the access event entity for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}-access"
        self._attr_translation_placeholders = {"door": door_label(hub, door_id)}

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def available(self) -> bool:
        return super().available and self._door is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to access events for this door."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DOOR_ACCESS.format(self._hub.entry.entry_id, self._door_id),
                self._handle_access,
            )
        )

    @callback
    def _handle_access(self, event: SiaEvent) -> None:
        event_type = DOOR_ACCESS_EVENT_TYPES.get(event.sia_code.upper())
        if event_type is None:
            return
        attributes: dict[str, str | None] = dict(access_attributes(event))
        if "user_id" in attributes:
            # Granted passages name the user; tell Home Assistant's own opens apart.
            attribution = self._hub.commands.attribute("door", self._door_id, event)
            attributes["source"] = attribution.source
            if attribution.changed_by:
                attributes["user"] = attribution.changed_by
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
