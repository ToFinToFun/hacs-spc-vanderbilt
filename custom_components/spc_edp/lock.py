"""Lock platform for Vanderbilt SPC (EDP) access-control doors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from spcedp import Door, Output, SiaEvent

from .commands import async_run_command
from .const import DOOR_MODE_SIA_CODES, SIGNAL_DOOR_ACCESS, SIGNAL_NEW_DOORS, SIGNAL_UPDATE_DOOR
from .door import async_set_door_mode, door_mode_option, door_status
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
class SpcEdpDoorLock(SpcEdpEntity, LockEntity, RestoreEntity):  # pylint: disable=abstract-method
    """One SPC door as a lock.

    The panel's locked mode (no access at all) and normal mode (access by card,
    PIN or exit button) both report `locked`; its open mode reports `unlocked`.
    The `door_mode` attribute tells the three apart. The panel does not report
    the lock output itself; when the door's lock output select names an output
    that follows it (such as a mapping gate), the state follows that output
    instead, including momentary unlocks. Lock returns an unlocked door to
    normal and leaves a locked door locked; open releases the lock for the
    panel's momentary unlock time.
    `changed_by` names who last changed the mode and `source` where it came
    from: Home Assistant, a panel user (keypad, app, reader), another EDP
    client or the panel itself (calendar, trigger).
    """

    _attr_supported_features = LockEntityFeature.OPEN
    _attr_translation_key = "door"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the lock for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}"
        self._attribution: dict[str, str | None] = {"changed_by_id": None, "source": None}

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
    def _lock_output(self) -> Output | None:
        panel = self._hub.panel
        output_id = self._hub.door_lock_outputs.get(self._door_id)
        return panel.outputs.get(output_id) if panel and output_id is not None else None

    @property
    def is_locked(self) -> bool | None:
        """Follow the lock output if one is set, else locked unless held unlocked."""
        output = self._lock_output
        if output is not None and output.is_on is not None:
            return not output.is_on
        option = door_mode_option(self._door)
        return None if option is None else option != "unlocked"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The panel's door mode, the lock output followed and who last changed the mode."""
        if self._door is None:
            return None
        output = self._lock_output
        return {
            "door_mode": door_mode_option(self._door),
            "door_status": door_status(self._door),
            "lock_output": output.name if output else None,
            **self._attribution,
        }

    async def async_added_to_hass(self) -> None:
        """Restore who last changed the mode and subscribe to door updates."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_changed_by = last.attributes.get("changed_by")
            for key in self._attribution:
                self._attribution[key] = last.attributes.get(key)
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
        """Record who changed the door mode and from where."""
        if event.sia_code.upper() not in DOOR_MODE_SIA_CODES:
            return
        attribution = self._hub.commands.attribute("door", self._door_id, event)
        self._attr_changed_by = attribution.changed_by
        self._attribution = attribution.as_attributes()
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        """Return an unlocked door to normal; a locked door stays locked."""
        self._connected_panel()
        if door_mode_option(self._door) == "unlocked":
            user = await self._async_command_user()
            await async_set_door_mode(self._hub, self._door_id, "normal", user)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Hold the door unlocked until its mode is changed again."""
        self._connected_panel()
        user = await self._async_command_user()
        await async_set_door_mode(self._hub, self._door_id, "unlocked", user)

    async def async_open(self, **kwargs: Any) -> None:
        """Release the lock for the panel's configured momentary unlock time."""
        control = self._connected_panel().door(self._door_id)
        self._hub.commands.note("door", self._door_id, await self._async_command_user())
        await async_run_command(control.open_momentary(), "opening this door")
