"""Sensors for Vanderbilt SPC (EDP) zone status and panel power readings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from spcedp import SystemStatus, ZoneInput, ZoneStatus
from spcedp.panel import Zone

from .const import SIGNAL_NEW_ZONES, SIGNAL_UPDATE_SYSTEM, SIGNAL_UPDATE_ZONE
from .entity import SpcEdpEntity, zone_label
from .hub import SpcEdpHub

# Zone STATUS and INPUT tokens as sensor options.
ZONE_STATUS_OPTIONS: dict[ZoneStatus, str] = {status: status.name.lower() for status in ZoneStatus}
ZONE_INPUT_OPTIONS: dict[ZoneInput, str] = {state: state.name.lower() for state in ZoneInput}


@dataclass(frozen=True, kw_only=True)
class PanelSensorDescription(SensorEntityDescription):
    """A reading from the panel's status reply."""

    value: Callable[[SystemStatus], float | None]


# Pylint does not see the keyword fields of Home Assistant's frozen entity descriptions.
# pylint: disable=unexpected-keyword-arg
PANEL_SENSORS: tuple[PanelSensorDescription, ...] = (
    PanelSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.battery_voltage,
    ),
    PanelSensorDescription(
        key="aux_voltage",
        translation_key="aux_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.aux_voltage,
    ),
    PanelSensorDescription(
        key="aux_current",
        translation_key="aux_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.aux_current,
    ),
    PanelSensorDescription(
        key="mains_frequency",
        translation_key="mains_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.ac_frequency,
    ),
)
# pylint: enable=unexpected-keyword-arg


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub], async_add_entities: AddEntitiesCallback
) -> None:
    """Set up panel readings and zone status sensors for known and future zones."""
    hub: SpcEdpHub = entry.runtime_data
    added: set[int] = set()

    @callback
    def _add_zones(zone_ids: set[int]) -> None:
        new = zone_ids - added
        if not new:
            return
        added.update(new)
        async_add_entities(
            entity
            for zone_id in new
            for entity in (
                SpcEdpZoneStatusSensor(hub, zone_id),
                SpcEdpZoneInputSensor(hub, zone_id),
            )
        )

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_ZONES.format(entry.entry_id), _add_zones)
    )
    async_add_entities(SpcEdpPanelSensor(hub, description) for description in PANEL_SENSORS)
    if hub.panel is not None:
        _add_zones(set(hub.panel.zones))


class SpcEdpPanelSensor(SpcEdpEntity, SensorEntity):
    """A power reading from the panel's status reply."""

    entity_description: PanelSensorDescription

    def __init__(self, hub: SpcEdpHub, description: PanelSensorDescription) -> None:
        """Initialize one panel reading."""
        super().__init__(hub)
        self.entity_description = description
        self._attr_unique_id = f"{hub.unique_id}-{description.key.replace('_', '-')}"

    @property
    def _status(self) -> SystemStatus | None:
        panel = self._hub.panel
        return panel.system if panel else None

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def native_value(self) -> float | None:
        status = self._status
        return self.entity_description.value(status) if status else None

    async def async_added_to_hass(self) -> None:
        """Subscribe to panel status reads."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_SYSTEM.format(self._hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )


class _ZoneSensor(SpcEdpEntity, SensorEntity):
    """An enum sensor for one zone, named by translation after it."""

    _suffix = ""
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, hub: SpcEdpHub, zone_id: int) -> None:
        """Initialize the sensor for one zone."""
        super().__init__(hub)
        self._zone_id = zone_id
        self._attr_unique_id = f"{hub.unique_id}-zone-{zone_id}-{self._suffix}"
        self._attr_translation_placeholders = {"zone": zone_label(hub, zone_id)}

    @property
    def _zone(self) -> Zone | None:
        panel = self._hub.panel
        return panel.zones.get(self._zone_id) if panel else None

    @property
    def available(self) -> bool:
        return super().available and self._zone is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to zone updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_ZONE.format(self._hub.entry.entry_id, self._zone_id),
                self.async_write_ha_state,
            )
        )


class SpcEdpZoneStatusSensor(_ZoneSensor):
    """The panel's status for the zone: normal, inhibited, isolated, alarm, tamper..."""

    _suffix = "status"
    _attr_translation_key = "zone_status"

    def __init__(self, hub: SpcEdpHub, zone_id: int) -> None:
        """Initialize the status sensor for one zone."""
        super().__init__(hub, zone_id)
        self._attr_options = list(ZONE_STATUS_OPTIONS.values())

    @property
    def native_value(self) -> str | None:
        zone = self._zone
        status = zone.zone_status if zone else None
        return ZONE_STATUS_OPTIONS[status] if status is not None else None


class SpcEdpZoneInputSensor(_ZoneSensor):
    """The zone's physical input: closed, open or a wiring/detector fault.

    The panel's undocumented LOGIC_INPUT, PROC_STATE and ALARM_STATE values
    are attributes, for diagnosis.
    """

    _suffix = "input"
    _attr_translation_key = "zone_input"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: SpcEdpHub, zone_id: int) -> None:
        """Initialize the input sensor for one zone."""
        super().__init__(hub, zone_id)
        self._attr_options = list(ZONE_INPUT_OPTIONS.values())

    @property
    def native_value(self) -> str | None:
        zone = self._zone
        state = zone.input_state if zone else None
        return ZONE_INPUT_OPTIONS[state] if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        zone = self._zone
        if zone is None:
            return {}
        return {
            "logic_input": zone.logic_input or None,
            "proc_state": zone.proc_state or None,
            "alarm_state": zone.alarm_state or None,
        }
