"""Shared entity base classes for the Vanderbilt SPC (EDP) integration."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from spcedp import Panel

from .const import DOMAIN, MANUFACTURER, SIGNAL_AVAILABILITY
from .hub import SpcEdpHub


def hub_device_info(hub: SpcEdpHub) -> DeviceInfo:
    """Device representing the physical SPC panel itself."""
    info = hub.panel.info if hub.panel else None
    device_info = DeviceInfo(
        identifiers={(DOMAIN, hub.unique_id)},
        manufacturer=MANUFACTURER,
        name=f"SPC{info.variant}" if info and info.variant else "SPC Panel",
        model=info.type if info else None,
        model_id=info.variant if info else None,
        sw_version=info.version if info else None,
        serial_number=info.sn if info else None,
    )
    if info and (info.hw_ver_major or info.hw_ver_minor):
        device_info["hw_version"] = f"{info.hw_ver_major}.{info.hw_ver_minor}"
    return device_info


class SpcEdpEntity(Entity):
    """Base entity: ties availability to the hub's live connection state."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hub: SpcEdpHub) -> None:
        """Initialize the entity with a reference to the shared hub."""
        self._hub = hub

    @property
    def available(self) -> bool:
        """Unavailable once the panel has been gone longer than the reconnect grace."""
        return self._hub.available

    def _connected_panel(self) -> Panel:
        """Return the live panel for a command; last-known state cannot take commands."""
        panel = self._hub.panel
        if not self.available or not self._hub.connected or panel is None:
            raise HomeAssistantError("The SPC panel is unavailable")
        return panel

    @property
    def device_info(self) -> DeviceInfo:
        return hub_device_info(self._hub)

    async def async_added_to_hass(self) -> None:
        """Subscribe to availability changes once added to hass."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_AVAILABILITY.format(self._hub.entry.entry_id),
                self._handle_availability_update,
            )
        )

    @callback
    def _handle_availability_update(self, _available: bool) -> None:
        self.async_write_ha_state()
