"""Lock platform for Vanderbilt SPC (EDP) access-control doors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from spcedp import Door, DoorMode
from spcedp.panel import DoorControl

from .commands import async_run_command
from .const import SIGNAL_NEW_DOORS, SIGNAL_UPDATE_DOOR
from .entity import SpcEdpEntity
from .hub import SpcEdpHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub], async_add_entities: AddEntitiesCallback
) -> None:
    """Set up lock entities for known and future doors."""
    hub: SpcEdpHub = entry.runtime_data
    added: set[int] = set()

    @callback
    def _add_doors(door_ids: set[int]) -> None:
        new = door_ids - added
        if not new:
            return
        added.update(new)
        async_add_entities(SpcEdpDoorLock(hub, door_id) for door_id in new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DOORS.format(entry.entry_id), _add_doors)
    )

    if hub.panel is not None:
        _add_doors(set(hub.panel.doors))


# Home Assistant supports async overrides and optional methods on this base class.
class SpcEdpDoorLock(SpcEdpEntity, LockEntity):  # pylint: disable=abstract-method
    """One SPC door.

    Normal (access-controlled) and locked modes keep the door locked; open mode
    keeps it unlocked. Opening releases the lock for the panel's unlock time.
    """

    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the lock for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}"

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def name(self) -> str:
        """Use the door's name from the panel (its door-position zone)."""
        door = self._door
        return door.name if door and door.name else f"Door {self._door_id}"

    @property
    def available(self) -> bool:
        return super().available and self._door is not None

    @property
    def is_locked(self) -> bool | None:
        """Locked unless the panel holds the door open."""
        door = self._door
        mode = door.door_mode if door else None
        if mode is None:
            return None
        return mode is not DoorMode.OPEN

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the raw door mode, which distinguishes normal from locked."""
        door = self._door
        if door is None:
            return None
        mode = door.door_mode
        return {"door_mode": mode.name.lower() if mode else None, "door_open": door.is_open}

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

    def _control(self) -> DoorControl:
        if not self.available or self._hub.panel is None:
            raise HomeAssistantError("The SPC panel is unavailable")
        return self._hub.panel.door(self._door_id)

    async def async_lock(self, **kwargs: Any) -> None:
        """Return the door to normal, access-controlled operation."""
        await async_run_command(self._control().set_normal(), "locking this door")
        await self._hub.async_refresh()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Hold the door open until it is locked again."""
        await async_run_command(self._control().open_permanent(), "unlocking this door")
        await self._hub.async_refresh()

    async def async_open(self, **kwargs: Any) -> None:
        """Release the lock for the panel's configured unlock time."""
        await async_run_command(self._control().open_momentary(), "opening this door")
