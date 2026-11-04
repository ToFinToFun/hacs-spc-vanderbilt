"""Select platform for Vanderbilt SPC (EDP) door modes and lock outputs."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from spcedp import Door

from .const import SIGNAL_NEW_DOORS, SIGNAL_NEW_OUTPUTS, SIGNAL_UPDATE_DOOR
from .door import DOOR_MODE_OPTIONS, async_set_door_mode, door_mode_option
from .entity import SpcEdpEntity, door_label
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
        async_add_entities(
            entity
            for door_id in new
            for entity in (
                SpcEdpDoorModeSelect(hub, door_id),
                SpcEdpDoorLockOutputSelect(hub, door_id),
            )
        )

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
        self._attr_translation_placeholders = {"door": door_label(hub, door_id)}

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

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
        user = await self._async_command_user()
        await async_set_door_mode(self._hub, self._door_id, option, user)


NO_OUTPUT = "none"


# Home Assistant supports async overrides and optional methods on this base class.
class SpcEdpDoorLockOutputSelect(SpcEdpEntity, SelectEntity, RestoreEntity):  # pylint: disable=abstract-method
    """Which panel output follows the door's lock, for the lock entity's state.

    The panel does not report a door controller's lock output. An output that
    follows it, typically a mapping gate, makes momentary unlocks visible.
    Outputs are named as in the panel; `none` uses the door mode.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "door_lock_output"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the lock output select for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}-lock-output"
        self._attr_translation_placeholders = {"door": door_label(hub, door_id)}
        # The chosen output's name, kept while the panel is not connected.
        self._choice = NO_OUTPUT

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def available(self) -> bool:
        return super().available and self._door is not None

    @property
    def options(self) -> list[str]:
        panel = self._hub.panel
        names = sorted({o.name for o in panel.outputs.values() if o.name}) if panel else []
        if self._choice != NO_OUTPUT and self._choice not in names:
            names.append(self._choice)
        return [NO_OUTPUT, *names]

    @property
    def current_option(self) -> str:
        return self._choice

    async def async_added_to_hass(self) -> None:
        """Restore the chosen output and follow output additions and renames."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state not in (
            "unknown",
            "unavailable",
        ):
            self._choice = last.state
        self._link()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NEW_OUTPUTS.format(self._hub.entry.entry_id),
                self._handle_outputs,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        self._hub.door_lock_outputs.pop(self._door_id, None)

    @callback
    def _handle_outputs(self, _output_ids: set[int]) -> None:
        self._link()
        self.async_write_ha_state()

    def _link(self) -> None:
        """Point the hub at the chosen output's ID, if the panel has it."""
        panel = self._hub.panel
        output_id = next(
            (
                output.id
                for output in (panel.outputs.values() if panel else ())
                if output.name == self._choice
            ),
            None,
        )
        if output_id is None:
            self._hub.door_lock_outputs.pop(self._door_id, None)
        else:
            self._hub.door_lock_outputs[self._door_id] = output_id

    async def async_select_option(self, option: str) -> None:
        """Follow this output for the door's lock state."""
        self._choice = option
        self._link()
        self.async_write_ha_state()
        async_dispatcher_send(
            self.hass, SIGNAL_UPDATE_DOOR.format(self._hub.entry.entry_id, self._door_id)
        )
