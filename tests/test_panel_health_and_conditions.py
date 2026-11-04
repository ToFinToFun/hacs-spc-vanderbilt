"""Zone faults and tamper, mapping gates, panel health, panel events and who changed an area."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from spcedp import SYSTEM_ALERTS, MappingGate, SystemStatus, Zone, parse_panel_time
from spcedp.events import SiaEvent
from spcedp.panel import Area

from custom_components.spc_edp.alarm_control_panel import SpcEdpAlarmControlPanel
from custom_components.spc_edp.attribution import CommandTracker
from custom_components.spc_edp.binary_sensor import (
    SpcEdpEngineerModeBinarySensor,
    SpcEdpMappingGateBinarySensor,
    SpcEdpSystemAlertBinarySensor,
    SpcEdpZoneFaultBinarySensor,
    SpcEdpZoneTamperBinarySensor,
    system_alert_key,
)
from custom_components.spc_edp.event import SpcEdpPanelEvent
from custom_components.spc_edp.sensor import (
    PANEL_SENSORS,
    SpcEdpPanelSensor,
    SpcEdpZoneInputSensor,
    SpcEdpZoneStatusSensor,
)


def _event(code: str, address: str, description: str = "") -> SiaEvent:
    return SiaEvent(
        spc_id=1000,
        timestamp=parse_panel_time("19011304112026"),
        timestamp_raw="19011304112026",
        sia_code=code,
        address=address,
        description=description,
        verification_id="0",
    )


def _zone(input_: str = "0", status: str = "0") -> Zone:
    return Zone(
        id=36,
        type="0",
        name="Vardagsrumsfönst",
        area_id=1,
        input=input_,
        status=status,
        logic_input="3",
        proc_state="2",
        alarm_state="0",
    )


def _hub(**panel: object) -> MagicMock:
    hub = MagicMock()
    hub.unique_id = "panel"
    hub.available = True
    hub.connected = True
    hub.panel = MagicMock()
    hub.panel.zones = {}
    hub.panel.areas = {}
    hub.panel.doors = {}
    hub.panel.mapping_gates = {}
    hub.panel.system = None
    for key, value in panel.items():
        setattr(hub.panel, key, value)
    hub.commands = CommandTracker()
    hub.door_lock_gates = {}
    return hub


@pytest.mark.parametrize(
    ("input_", "status", "fault", "tamper"),
    [
        ("0", "0", None, False),
        ("1", "0", None, False),
        ("3", "2", "disconnect", False),
        ("6", "2", "sensor_missing", False),
        ("2", "0", "short", False),
        ("0", "7", "trouble", False),
        ("0", "4", None, True),
    ],
)
def test_zone_fault_and_tamper(input_: str, status: str, fault: str | None, tamper: bool) -> None:
    hub = _hub(zones={36: _zone(input_, status)})
    fault_sensor = SpcEdpZoneFaultBinarySensor(hub, 36)
    tamper_sensor = SpcEdpZoneTamperBinarySensor(hub, 36)
    assert fault_sensor.is_on is (fault is not None)
    assert fault_sensor.extra_state_attributes == {"fault": fault}
    assert fault_sensor.translation_placeholders == {"zone": "Vardagsrumsfönst"}
    assert tamper_sensor.is_on is tamper


def test_zone_status_and_input_sensors() -> None:
    hub = _hub(zones={36: _zone("3", "2")})
    status = SpcEdpZoneStatusSensor(hub, 36)
    input_sensor = SpcEdpZoneInputSensor(hub, 36)
    assert status.native_value == "isolate"
    assert "tamper" in status.options
    assert input_sensor.native_value == "disconnect"
    assert input_sensor.extra_state_attributes == {
        "logic_input": "3",
        "proc_state": "2",
        "alarm_state": "0",
    }
    assert _hub().panel.zones.get(36) is None
    assert SpcEdpZoneStatusSensor(_hub(), 36).available is False


def test_mapping_gate_and_panel_health() -> None:
    status = SystemStatus(
        engineer_mode="1",
        battery_voltage_raw="13.5V",
        aux_current_raw="220mA",
        ac_frequency_raw="50Hz",
    )
    hub = _hub(
        mapping_gates={3: MappingGate(id=3, name="Entredörr upplås", state="1")}, system=status
    )
    gate = SpcEdpMappingGateBinarySensor(hub, 3)
    assert (gate.name, gate.is_on) == ("Entredörr upplås", True)
    assert SpcEdpEngineerModeBinarySensor(hub).is_on is True
    values = {d.key: SpcEdpPanelSensor(hub, d).native_value for d in PANEL_SENSORS}
    assert values == {
        "battery_voltage": 13.5,
        "aux_voltage": None,
        "aux_current": 220.0,
        "ac_frequency": 50.0,
    }
    assert SpcEdpEngineerModeBinarySensor(_hub()).available is False


def _system_alert(alert_id: int):
    return next(alert for alert in SYSTEM_ALERTS if alert.id == alert_id)


@pytest.mark.parametrize(
    ("masks", "alert_id", "on", "status"),
    [
        (("00000000", "00000000", "00000000", "01000000"), 38, False, "isolate"),
        (("00000000", "00000000", "00400000", "00000000"), 36, False, "inhibit"),
        (("00200000", "00200000", "00000000", "00000000"), 2, True, "alert"),
        (("00000001", "00000000", "00000000", "00000000"), 0, True, "ok"),
        (("00000000", "00000000", "00000000", "00000000"), 7, False, "ok"),
    ],
)
def test_system_alerts_follow_the_status_masks(
    masks: tuple[str, str, str, str], alert_id: int, on: bool, status: str
) -> None:
    system = SystemStatus(
        alert_input=masks[0], alert=masks[1], alert_inhibit=masks[2], alert_isolate=masks[3]
    )
    sensor = SpcEdpSystemAlertBinarySensor(_hub(system=system), _system_alert(alert_id))
    assert sensor.is_on is on
    assert sensor.extra_state_attributes == {"status": status}


def test_system_alert_names_and_classes() -> None:
    assert system_alert_key(_system_alert(3)) == "controller_aux_fuse_fault"
    assert system_alert_key(_system_alert(20)) == "x_bus_cable_fault"
    hub = _hub(system=SystemStatus())
    assert SpcEdpSystemAlertBinarySensor(hub, _system_alert(7)).device_class == "tamper"
    assert SpcEdpSystemAlertBinarySensor(hub, _system_alert(0)).device_class == "problem"
    assert SpcEdpSystemAlertBinarySensor(hub, _system_alert(0)).available is False
    assert SpcEdpSystemAlertBinarySensor(hub, _system_alert(0)).unique_id == "panel-system-alert-0"


def test_panel_event_names_code_target_and_user() -> None:
    hub = _hub(zones={36: _zone()})
    entity = SpcEdpPanelEvent(hub)
    entity._trigger_event = MagicMock()  # type: ignore[method-assign]
    entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    entity._handle_event(_event("ZD", "36", "Vardagsrumsfönst"))
    event_type, attributes = entity._trigger_event.call_args.args
    assert event_type == "zd"
    assert attributes["name"] == "Zone disconnected"
    assert attributes["zone"] == "Vardagsrumsfönst"
    assert attributes["panel_time"].startswith("2026-11-04T19:01:13")
    assert "user" not in attributes
    entity._handle_event(_event("JT", "0", "Panel¦Alice¦1"))
    event_type, attributes = entity._trigger_event.call_args.args
    assert (event_type, attributes["user"], attributes["user_id"]) == ("jt", "Alice", "1")
    entity._handle_event(_event("Q9", "1"))
    assert entity._trigger_event.call_args.args[0] == "unknown"
    assert "unknown" in entity.event_types and "dg" in entity.event_types


def test_home_assistant_user_is_credited_for_its_commands() -> None:
    tracker = CommandTracker()
    tracker.note("area", 3, "Jerry")
    attribution = tracker.attribute_user("area", 3, "Fjärrinstallatör", "9998")
    assert (attribution.changed_by, attribution.source) == ("Jerry", "home_assistant")
    tracker.note("door", 1)
    assert tracker.attribute_user("door", 1, "admin", "9998").changed_by == "Home Assistant"
    assert tracker.attribute_user("area", 2, "Fjärrinstallatör", "9998").source == "remote"


def test_area_credits_the_panels_record_of_the_last_change() -> None:
    area = Area(
        id=3,
        name="Förråd / Ute",
        mode="0",
        last_set_time="16571403112026",
        last_set_user_id="1",
        last_set_user_name="Alice",
        last_unset_time="16572003112026",
        last_unset_user_id="9998",
        last_unset_user_name="Fjärrinstallatör",
    )
    hub = _hub(areas={3: area})
    entity = SpcEdpAlarmControlPanel(hub, 3)
    entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    entity._handle_area_update()
    # The latest change is the unset, by an EDP client other than Home Assistant.
    assert entity.changed_by == "Fjärrinstallatör"
    attributes = entity.extra_state_attributes
    assert attributes["source"] == "remote"
    assert attributes["last_set_user_name"] == "Alice"
    assert attributes["last_unset_time"].startswith("2026-11-03T16:57:20")
    # A later set at a keypad is credited once the panel records it.
    area.last_set_time = "17000003112026"
    entity._handle_area_update()
    assert (entity.changed_by, entity.extra_state_attributes["source"]) == ("Alice", "panel")
    # The same record is not credited twice, so an event's attribution stands.
    entity._attr_changed_by = "Someone else"
    entity._handle_area_update()
    assert entity.changed_by == "Someone else"


@pytest.mark.asyncio
async def test_command_user_comes_from_the_service_context(hass) -> None:
    user = await hass.auth.async_create_user("Jerry")
    entity = SpcEdpMappingGateBinarySensor(_hub(), 1)
    entity.hass = hass
    assert await entity._async_command_user() is None
    entity.async_set_context(MagicMock(user_id=user.id))
    assert await entity._async_command_user() == "Jerry"
