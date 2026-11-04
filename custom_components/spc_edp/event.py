"""Event platform for Vanderbilt SPC (EDP) door access."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from spcedp import Door, SiaEvent

from .const import (
    ACCESS_DENIED_REASONS,
    DOOR_ACCESS_EVENT_TYPES,
    SIGNAL_DOOR_ACCESS,
    SIGNAL_NEW_DOORS,
)
from .entity import SpcEdpEntity
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

    if hub.panel is not None:
        _add_doors(set(hub.panel.doors))


class SpcEdpDoorAccessEvent(SpcEdpEntity, EventEntity):
    """Access granted, denied and exit requests reported for one door."""

    _attr_event_types = sorted(set(DOOR_ACCESS_EVENT_TYPES.values()))
    _attr_translation_key = "door_access"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the access event entity for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}-access"

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def name(self) -> str:
        door = self._door
        return f"{door.name if door and door.name else f'Door {self._door_id}'} access"

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
