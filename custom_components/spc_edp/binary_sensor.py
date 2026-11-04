"""Binary sensors for Vanderbilt SPC (EDP) zones, doors, mapping gates and panel health."""

from __future__ import annotations

import re

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from spcedp import (
    SYSTEM_ALERTS,
    Door,
    MappingGate,
    SiaEvent,
    SystemAlertInfo,
    SystemAlertState,
    ZoneStatus,
    ZoneType,
)
from spcedp.panel import Zone

from .const import (
    DOOR_ACCESS_EVENT_TYPES,
    DOOR_ALARM_CLEAR_CODES,
    DOOR_ALARM_SET_CODES,
    SIGNAL_DOOR_ACCESS,
    SIGNAL_NEW_DOORS,
    SIGNAL_NEW_MAPPING_GATES,
    SIGNAL_NEW_ZONES,
    SIGNAL_UPDATE_DOOR,
    SIGNAL_UPDATE_MAPPING_GATE,
    SIGNAL_UPDATE_SYSTEM,
    SIGNAL_UPDATE_ZONE,
)
from .entity import SpcEdpEntity, door_label, zone_label
from .hub import SpcEdpHub
from .sensor import ZONE_INPUT_OPTIONS

# This remains an adapter concern: Home Assistant's entity device classes are
# not part of the protocol SDK. The SDK owns the complete ZoneType vocabulary.
_DEVICE_CLASS_BY_TYPE: dict[ZoneType, BinarySensorDeviceClass] = {
    ZoneType.ALARM: BinarySensorDeviceClass.MOTION,
    ZoneType.ENTRY_EXIT: BinarySensorDeviceClass.DOOR,
    ZoneType.ENTRY_EXIT_2: BinarySensorDeviceClass.DOOR,
    ZoneType.FIRE: BinarySensorDeviceClass.SMOKE,
    ZoneType.PANIC: BinarySensorDeviceClass.SAFETY,
    ZoneType.HOLD_UP: BinarySensorDeviceClass.SAFETY,
    ZoneType.TAMPER: BinarySensorDeviceClass.TAMPER,
    ZoneType.TECHNICAL: BinarySensorDeviceClass.POWER,
    ZoneType.MEDICAL: BinarySensorDeviceClass.SAFETY,
    ZoneType.GLASSBREAK: BinarySensorDeviceClass.VIBRATION,
    ZoneType.WATER: BinarySensorDeviceClass.MOISTURE,
    ZoneType.HEAT: BinarySensorDeviceClass.HEAT,
    ZoneType.FRIDGE_FREEZER: BinarySensorDeviceClass.COLD,
    ZoneType.GAS: BinarySensorDeviceClass.GAS,
    ZoneType.CO: BinarySensorDeviceClass.CO,
    ZoneType.SEISMIC: BinarySensorDeviceClass.VIBRATION,
}


def _device_class(zone_type: ZoneType | None) -> BinarySensorDeviceClass | None:
    return _DEVICE_CLASS_BY_TYPE.get(zone_type) if zone_type is not None else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub], async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensor entities for known and future zones."""
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
                SpcEdpZoneBinarySensor(hub, zone_id),
                SpcEdpZoneFaultBinarySensor(hub, zone_id),
                SpcEdpZoneTamperBinarySensor(hub, zone_id),
            )
        )

    added_doors: set[int] = set()

    @callback
    def _add_doors(door_ids: set[int]) -> None:
        new = door_ids - added_doors
        if not new:
            return
        added_doors.update(new)
        async_add_entities(
            entity
            for door_id in new
            for entity in (
                SpcEdpDoorAlarmBinarySensor(hub, door_id),
                SpcEdpDoorDrsBinarySensor(hub, door_id),
            )
        )

    added_gates: set[int] = set()

    @callback
    def _add_gates(gate_ids: set[int]) -> None:
        new = gate_ids - added_gates
        if not new:
            return
        added_gates.update(new)
        async_add_entities(SpcEdpMappingGateBinarySensor(hub, gate_id) for gate_id in new)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_ZONES.format(entry.entry_id), _add_zones)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DOORS.format(entry.entry_id), _add_doors)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_MAPPING_GATES.format(entry.entry_id), _add_gates)
    )

    async_add_entities(
        [
            SpcEdpEngineerModeBinarySensor(hub),
            *(SpcEdpSystemAlertBinarySensor(hub, alert) for alert in SYSTEM_ALERTS),
        ]
    )
    if hub.panel is not None:
        _add_zones(set(hub.panel.zones))
        _add_doors(set(hub.panel.doors))
        _add_gates(set(hub.panel.mapping_gates))


class SpcEdpZoneBinarySensor(SpcEdpEntity, BinarySensorEntity):
    """Representation of one SPC detection zone."""

    def __init__(self, hub: SpcEdpHub, zone_id: int) -> None:
        """Initialize the binary sensor for one zone."""
        super().__init__(hub)
        self._zone_id = zone_id
        self._attr_unique_id = f"{hub.unique_id}-zone-{zone_id}"

    @property
    def _zone(self) -> Zone | None:
        panel = self._hub.panel
        return panel.zones.get(self._zone_id) if panel else None

    @property
    def name(self) -> str:
        """Use the zone's real name from the panel (e.g. "Front Door")."""
        zone = self._zone
        return zone.name if zone and zone.name else f"Zone {self._zone_id}"

    @property
    def available(self) -> bool:
        return super().available and self._zone is not None

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        """Best-effort device class derived from the SDK's typed zone type."""
        zone = self._zone
        return _device_class(zone.zone_type) if zone else None

    @property
    def is_on(self) -> bool | None:
        """Whether the zone's physical input is open/active."""
        zone = self._zone
        if zone is None:
            return None
        return zone.is_open

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


class SpcEdpDoorAlarmBinarySensor(SpcEdpEntity, BinarySensorEntity, RestoreEntity):
    """On while the panel reports the door forced open or left open.

    Set by the DF/DN access events and cleared by DR; the panel offers no read
    of this state, so it is restored across Home Assistant restarts.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "door_alarm"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        """Initialize the alarm sensor for one door."""
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}-alarm"
        self._attr_translation_placeholders = {"door": door_label(hub, door_id)}
        self._attr_is_on = False
        self._condition: str | None = None

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def available(self) -> bool:
        return super().available and self._door is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"condition": self._condition}

    async def async_added_to_hass(self) -> None:
        """Restore the last alarm state and subscribe to door events."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
            self._condition = last.attributes.get("condition")
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DOOR_ACCESS.format(self._hub.entry.entry_id, self._door_id),
                self._handle_door_event,
            )
        )

    @callback
    def _handle_door_event(self, event: SiaEvent) -> None:
        code = event.sia_code.upper()
        if code in DOOR_ALARM_SET_CODES:
            self._attr_is_on = True
            self._condition = DOOR_ACCESS_EVENT_TYPES[code]
        elif code in DOOR_ALARM_CLEAR_CODES:
            self._attr_is_on = False
            self._condition = None
        else:
            return
        self.async_write_ha_state()


class _ZoneEntity(SpcEdpEntity):
    """A zone-derived entity, named by translation after its zone."""

    _suffix = ""

    def __init__(self, hub: SpcEdpHub, zone_id: int) -> None:
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
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_ZONE.format(self._hub.entry.entry_id, self._zone_id),
                self.async_write_ha_state,
            )
        )


class SpcEdpZoneFaultBinarySensor(_ZoneEntity, BinarySensorEntity):
    """On while the zone's input reports a wiring or detector fault, or trouble.

    `fault` names it with the input state (short, disconnect, masked, dc_sub,
    sensor_missing, offline) or the zone status `trouble`.
    """

    _suffix = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "zone_fault"

    @property
    def _fault(self) -> str | None:
        zone = self._zone
        if zone is None:
            return None
        if (fault := zone.fault) is not None:
            return ZONE_INPUT_OPTIONS[fault]
        return "trouble" if zone.zone_status is ZoneStatus.TROUBLE else None

    @property
    def is_on(self) -> bool | None:
        return None if self._zone is None else self._fault is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"fault": self._fault}


class SpcEdpZoneTamperBinarySensor(_ZoneEntity, BinarySensorEntity):
    """On while the panel reports the zone in tamper."""

    _suffix = "tamper"
    _attr_device_class = BinarySensorDeviceClass.TAMPER
    _attr_translation_key = "zone_tamper"

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        return None if zone is None else zone.zone_status is ZoneStatus.TAMPER


class SpcEdpDoorDrsBinarySensor(SpcEdpEntity, BinarySensorEntity):
    """The door's DRS (door release switch) input as the panel reads it."""

    _attr_translation_key = "door_drs"

    def __init__(self, hub: SpcEdpHub, door_id: int) -> None:
        super().__init__(hub)
        self._door_id = door_id
        self._attr_unique_id = f"{hub.unique_id}-door-{door_id}-drs"
        self._attr_translation_placeholders = {"door": door_label(hub, door_id)}

    @property
    def _door(self) -> Door | None:
        panel = self._hub.panel
        return panel.doors.get(self._door_id) if panel else None

    @property
    def available(self) -> bool:
        return super().available and self._door is not None

    @property
    def is_on(self) -> bool | None:
        door = self._door
        return None if door is None else {"1": True, "0": False}.get(door.drs_input)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_DOOR.format(self._hub.entry.entry_id, self._door_id),
                self.async_write_ha_state,
            )
        )


class SpcEdpMappingGateBinarySensor(SpcEdpEntity, BinarySensorEntity):
    """One mapping gate (web interface: Outputs > Mapping Gate), named as in the panel."""

    def __init__(self, hub: SpcEdpHub, gate_id: int) -> None:
        super().__init__(hub)
        self._gate_id = gate_id
        self._attr_unique_id = f"{hub.unique_id}-mapping-gate-{gate_id}"

    @property
    def _gate(self) -> MappingGate | None:
        panel = self._hub.panel
        return panel.mapping_gates.get(self._gate_id) if panel else None

    @property
    def name(self) -> str:
        gate = self._gate
        return gate.name if gate and gate.name else f"Mapping gate {self._gate_id}"

    @property
    def available(self) -> bool:
        return super().available and self._gate is not None

    @property
    def is_on(self) -> bool | None:
        gate = self._gate
        return None if gate is None else gate.is_on

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_MAPPING_GATE.format(self._hub.entry.entry_id, self._gate_id),
                self.async_write_ha_state,
            )
        )


class SpcEdpEngineerModeBinarySensor(SpcEdpEntity, BinarySensorEntity):
    """On while the panel is in engineer mode: it sends no events and refuses commands."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "engineer_mode"

    def __init__(self, hub: SpcEdpHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.unique_id}-engineer-mode"

    @property
    def available(self) -> bool:
        panel = self._hub.panel
        return super().available and panel is not None and panel.system is not None

    @property
    def is_on(self) -> bool | None:
        panel = self._hub.panel
        return panel.system.in_engineer_mode if panel and panel.system else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_SYSTEM.format(self._hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )


def system_alert_key(alert: SystemAlertInfo) -> str:
    """Translation key from the alert's name, e.g. `controller_aux_fuse_fault`."""
    return re.sub(r"[^a-z0-9]+", "_", alert.name.lower()).strip("_")


# System alert Status values, as the panel's web interface shows them.
SYSTEM_ALERT_STATUS_OK = "ok"


class SpcEdpSystemAlertBinarySensor(SpcEdpEntity, BinarySensorEntity):
    """One of the panel's system alerts (web interface: Status > System Alerts).

    On while its Input reports the fault or tamper; `status` is the alert's
    Status column: `ok`, `alert`, `inhibit` or `isolate`.
    """

    def __init__(self, hub: SpcEdpHub, alert: SystemAlertInfo) -> None:
        super().__init__(hub)
        self._alert = alert
        key = system_alert_key(alert)
        self._attr_unique_id = f"{hub.unique_id}-system-alert-{alert.id}"
        self._attr_translation_key = key
        self._attr_device_class = (
            BinarySensorDeviceClass.TAMPER if "tamper" in key else BinarySensorDeviceClass.PROBLEM
        )

    @property
    def _state(self) -> SystemAlertState | None:
        panel = self._hub.panel
        return panel.system.system_alert(self._alert) if panel and panel.system else None

    @property
    def available(self) -> bool:
        return super().available and self._state is not None

    @property
    def is_on(self) -> bool | None:
        state = self._state
        return None if state is None else state.input

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        state = self._state
        if state is None:
            return {"status": None}
        if state.isolated:
            status = "isolate"
        elif state.inhibited:
            status = "inhibit"
        elif state.alert:
            status = "alert"
        else:
            status = SYSTEM_ALERT_STATUS_OK
        return {"status": status}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_SYSTEM.format(self._hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )
