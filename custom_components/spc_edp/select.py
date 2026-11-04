"""Select platform for the mode of Vanderbilt SPC (EDP) access-control doors."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from spcedp import Door

from .const import SIGNAL_NEW_DOORS, SIGNAL_UPDATE_DOOR
from .door import DOOR_MODE_OPTIONS, async_set_door_mode, door_mode_option
from .entity import SpcEdpEntity
from .hub import SpcEdpHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub], async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a door mode select for known and future doors."""
    hub: SpcEdpHub = entry.runtime_data
    added: set[int] = set()

    @callback
    def _add_doors(door_ids: set[int]) -> None:
        new = door_ids - added
        if not new:
            return
        added.update(new)
        async_add_entities(SpcEdpDoorModeSelect(hub, door_id) for door_id in new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DOORS.format(entry.entry_id), _add_doors)
    )

    if hub.panel is not None:
        _add_doors(set(hub.panel.doors))


# Home Assistant supports async overrides and optional methods on this base class.
class SpcEdpDoorModeSelect(SpcEdpEntity, SelectEntity):  # pylint: disable=abstract-method
    """The door's mode: locked (no access), normal (access control) or unlocked."""

    _attr_translation_key = "door_mode"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the mode select for one door."""
        super().__init__(hub)
        self._attr_options = list(DOOR_MODE_OPTIONS.values())
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}-mode"

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def name(self) -> str:
        door = self._door
        return f"{door.name if door and door.name else f'Door {self._door_id}'} mode"

    @property
    def available(self) -> bool:
        return super().available and self._door is not None

    @property
    def current_option(self) -> str | None:
        return door_mode_option(self._door)

    async def async_added_to_hass(self) -> None:
        """Subscribe to door-specific + availability updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_DOOR.format(self._hub.entry.entry_id, self._door_id),
                self.async_write_ha_state,
            )
        )

    async def async_select_option(self, option: str) -> None:
        """Set the door to the selected mode."""
        self._connected_panel()
        await async_set_door_mode(self._hub, self._door_id, option)
