"""Lock platform for Vanderbilt SPC (EDP) access-control doors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from spcedp import Door, DoorMode, SiaEvent
from spcedp.panel import DoorControl

from .commands import async_run_command
from .const import DOOR_MODE_SIA_CODES, SIGNAL_DOOR_ACCESS, SIGNAL_NEW_DOORS, SIGNAL_UPDATE_DOOR
from .entity import SpcEdpEntity
from .event import access_attributes
from .hub import SpcEdpHub

ATTR_CHANGED_BY_ID = "changed_by_id"


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
class SpcEdpDoorLock(SpcEdpEntity, LockEntity, RestoreEntity):  # pylint: disable=abstract-method
    """One SPC door.

    Normal (access-controlled) and locked modes keep the door locked; open mode
    keeps it unlocked. Opening releases the lock for the panel's unlock time.
    `changed_by` names the user the panel reported for the last mode change;
    it is empty when the panel changed the mode itself (calendar or trigger).
    """

    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the lock for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}"
        self._changed_by_id: str | None = None

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
        return {
            "door_mode": mode.name.lower() if mode else None,
            "door_open": door.is_open,
            ATTR_CHANGED_BY_ID: self._changed_by_id,
        }

    async def async_added_to_hass(self) -> None:
        """Restore who last changed the mode and subscribe to door updates."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_changed_by = last.attributes.get("changed_by")
            self._changed_by_id = last.attributes.get(ATTR_CHANGED_BY_ID)
        entry_id = self._hub.entry.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_DOOR.format(entry_id, self._door_id),
                self.async_write_ha_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DOOR_ACCESS.format(entry_id, self._door_id),
                self._handle_door_event,
            )
        )

    @callback
    def _handle_door_event(self, event: SiaEvent) -> None:
        """Record who the panel says changed the door mode."""
        if event.sia_code.upper() not in DOOR_MODE_SIA_CODES:
            return
        fields = access_attributes(event)
        self._attr_changed_by = fields.get("user")
        user_id = fields.get("user_id")
        self._changed_by_id = user_id if user_id and user_id != "0" else None
        self.async_write_ha_state()

    def _control(self) -> DoorControl:
        return self._connected_panel().door(self._door_id)

    def _mode(self) -> DoorMode | None:
        door = self._door
        return door.door_mode if door else None

    async def async_lock(self, **kwargs: Any) -> None:
        """Return the door to normal, access-controlled operation."""
        control = self._control()
        if self._mode() is DoorMode.NORMAL:
            return  # the panel rejects setting a door to its current mode
        await async_run_command(control.set_normal(), "locking this door")
        await self._hub.async_refresh()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Hold the door open until it is locked again."""
        control = self._control()
        if self._mode() is DoorMode.OPEN:
            return  # the panel rejects setting a door to its current mode
        await async_run_command(control.open_permanent(), "unlocking this door")
        await self._hub.async_refresh()

    async def async_open(self, **kwargs: Any) -> None:
        """Release the lock for the panel's configured unlock time."""
        await async_run_command(self._control().open_momentary(), "opening this door")
