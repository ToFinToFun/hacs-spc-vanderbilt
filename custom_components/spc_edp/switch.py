"""Switch platform for isolating Vanderbilt SPC (EDP) zones."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from spcedp import ZoneStatus
from spcedp.panel import Zone

from .commands import async_run_command
from .const import SIGNAL_NEW_ZONES, SIGNAL_UPDATE_ZONE
from .entity import SpcEdpEntity
from .hub import SpcEdpHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub], async_add_entities: AddEntitiesCallback
) -> None:
    """Set up isolate switches for known and future zones."""
    hub: SpcEdpHub = entry.runtime_data
    added: set[int] = set()

    @callback
    def _add_zones(zone_ids: set[int]) -> None:
        new = zone_ids - added
        if not new:
            return
        added.update(new)
        async_add_entities(SpcEdpZoneIsolateSwitch(hub, zone_id) for zone_id in new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_ZONES.format(entry.entry_id), _add_zones)
    )

    if hub.panel is not None:
        _add_zones(set(hub.panel.zones))


# Home Assistant supports async overrides and optional methods on this base class.
class SpcEdpZoneIsolateSwitch(SpcEdpEntity, SwitchEntity):  # pylint: disable=abstract-method
    """On while the zone is bypassed (isolated or inhibited) at the panel."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: SpcEdpHub, zone_id: int) -> None:
        """Initialize the isolate switch for one zone."""
        super().__init__(hub)
        self._zone_id = zone_id
        self._attr_unique_id = f"{hub.unique_id}-zone-{zone_id}-isolate"

    @property
    def _zone(self) -> Zone | None:
        panel = self._hub.panel
        return panel.zones.get(self._zone_id) if panel else None

    @property
    def name(self) -> str:
        zone = self._zone
        return f"{zone.name if zone and zone.name else f'Zone {self._zone_id}'} isolated"

    @property
    def available(self) -> bool:
        return super().available and self._zone is not None

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        status = zone.zone_status if zone else None
        if status is None:
            return None
        return status is not ZoneStatus.NORMAL

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        zone = self._zone
        status = zone.zone_status if zone else None
        return {"zone_status": status.name.lower()} if status else None

    async def async_added_to_hass(self) -> None:
        """Subscribe to zone-specific + availability updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_ZONE.format(self._hub.entry.entry_id, self._zone_id),
                self.async_write_ha_state,
            )
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Isolate the zone until it is restored."""
        if not self.available or self._hub.panel is None:
            raise HomeAssistantError("The SPC panel is unavailable")
        zone = self._hub.panel.zone(self._zone_id)
        await async_run_command(zone.isolate(), "isolating this zone")
        await self._hub.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Restore the zone from whichever bypass the panel reports."""
        current = self._zone
        if not self.available or self._hub.panel is None or current is None:
            raise HomeAssistantError("The SPC panel is unavailable")
        zone = self._hub.panel.zone(self._zone_id)
        if current.zone_status is ZoneStatus.INHIBITED:
            await async_run_command(zone.deinhibit(), "restoring this zone")
        else:
            await async_run_command(zone.deisolate(), "restoring this zone")
        await self._hub.async_refresh()
