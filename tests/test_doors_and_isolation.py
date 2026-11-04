"""Door locks, door modes, access events, door alarms and zone isolate switches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from spcedp import Door, Zone
from spcedp.events import SiaEvent
from spcedp.panel import EventStateUpdate

from custom_components.spc_edp.attribution import CommandTracker
from custom_components.spc_edp.binary_sensor import SpcEdpDoorAlarmBinarySensor
from custom_components.spc_edp.const import (
    SIGNAL_AREA_CHANGE,
    SIGNAL_DOOR_ACCESS,
    SIGNAL_UPDATE_DOOR,
)
from custom_components.spc_edp.event import SpcEdpDoorAccessEvent, access_attributes
from custom_components.spc_edp.hub import SpcEdpHub
from custom_components.spc_edp.lock import SpcEdpDoorLock
from custom_components.spc_edp.select import SpcEdpDoorModeSelect
from custom_components.spc_edp.switch import SpcEdpZoneIsolateSwitch

# Placeholder users and cards only; never real access-control data.
GRANTED = "Entredörr¦Alice¦1¦123456¦ENTRY"


def _event(code: str, address: str, description: str, at: str = "16481003112026") -> SiaEvent:
    return SiaEvent(
        spc_id=4567,
        timestamp=None,
        timestamp_raw=at,
        sia_code=code,
        address=address,
        description=description,
        verification_id="0",
    )


def _door(mode: str, dps: str = "0") -> Door:
    return Door(
        id=3,
        name="Entredörr",
        zone_id=25,
        area_id=1,
        mode=mode,
        status="0",
        dps_input=dps,
        drs_input="0",
    )


def _hub(*, doors: dict | None = None, zones: dict | None = None) -> MagicMock:
    hub = MagicMock()
    hub.unique_id = "panel"
    hub.available = True
    hub.connected = True
    hub.panel = MagicMock()
    hub.panel.doors = doors or {}
    hub.panel.zones = zones or {}
    hub.panel.outputs = {}
    hub.door_lock_outputs = {}
    hub.async_refresh = AsyncMock()
    hub.commands = CommandTracker()
    return hub


def _door_controls(hub: MagicMock) -> MagicMock:
    control = hub.panel.door.return_value
    for name in ("lock", "set_normal", "open_permanent", "open_momentary"):
        setattr(control, name, AsyncMock())
    return control


@pytest.mark.parametrize(
    ("mode", "locked", "door_mode"),
    [("0", True, "normal"), ("1", True, "locked"), ("2", False, "unlocked"), ("9", None, None)],
)
def test_lock_and_mode_select_follow_the_door_mode(
    mode: str, locked: bool | None, door_mode: str | None
) -> None:
    hub = _hub(doors={3: _door(mode, dps="1")})
    lock = SpcEdpDoorLock(hub, 3)
    select = SpcEdpDoorModeSelect(hub, 3)
    assert lock.is_locked is locked
    # Whether the door stands open is its position zone, not a lock attribute.
    assert lock.extra_state_attributes == {
        "door_mode": door_mode,
        "door_status": "ok",
        "lock_output": None,
        "changed_by_id": None,
        "source": None,
    }
    assert select.current_option == door_mode
    assert select.options == ["locked", "normal", "unlocked"]
    assert lock.name == "Entredörr"
    assert select.translation_placeholders == {"door": "Entredörr"}


def test_door_entities_are_unavailable_for_a_missing_door() -> None:
    hub = _hub()
    assert SpcEdpDoorLock(hub, 3).available is False
    assert SpcEdpDoorLock(hub, 3).is_locked is None
    assert SpcEdpDoorModeSelect(hub, 3).available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "action", "sent"),
    [
        ("2", "async_lock", "set_normal"),
        ("0", "async_lock", None),  # already normal
        ("1", "async_lock", None),  # locking never lifts the locked mode
        ("0", "async_unlock", "open_permanent"),
        ("1", "async_unlock", "open_permanent"),
        ("2", "async_unlock", None),  # already unlocked
        ("0", "async_open", "open_momentary"),
    ],
)
async def test_lock_actions(mode: str, action: str, sent: str | None) -> None:
    hub = _hub(doors={3: _door(mode)})
    control = _door_controls(hub)
    await getattr(SpcEdpDoorLock(hub, 3), action)()
    for name in ("lock", "set_normal", "open_permanent", "open_momentary"):
        assert getattr(control, name).await_count == (1 if name == sent else 0), name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "option", "sent"),
    [
        ("0", "locked", "lock"),
        ("1", "normal", "set_normal"),
        ("0", "unlocked", "open_permanent"),
        ("1", "locked", None),  # the panel rejects the current mode
    ],
)
async def test_mode_select_sets_the_door_mode(mode: str, option: str, sent: str | None) -> None:
    hub = _hub(doors={3: _door(mode)})
    control = _door_controls(hub)
    await SpcEdpDoorModeSelect(hub, 3).async_select_option(option)
    for name in ("lock", "set_normal", "open_permanent"):
        assert getattr(control, name).await_count == (1 if name == sent else 0), name
    assert hub.async_refresh.await_count == (1 if sent else 0)


@pytest.mark.asyncio
async def test_commands_need_a_live_link_during_the_reconnect_grace() -> None:
    hub = _hub(doors={3: _door("0")})
    hub.connected = False
    lock = SpcEdpDoorLock(hub, 3)
    assert lock.available
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await lock.async_unlock()
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await SpcEdpDoorModeSelect(hub, 3).async_select_option("locked")


@pytest.mark.parametrize(
    ("description", "ours", "changed_by", "changed_by_id", "source"),
    [
        ("Entredörr¦admin¦9998¦0", True, "Home Assistant", "9998", "home_assistant"),
        ("Entredörr¦admin¦9998¦0", False, "admin", "9998", "remote"),
        ("Entredörr¦Alice¦1¦123456", True, "Alice", "1", "panel"),
        ("Entredörr¦¦0¦0", False, None, None, "panel_automatic"),
    ],
)
def test_lock_records_who_changed_the_mode_and_from_where(
    description: str, ours: bool, changed_by: str | None, changed_by_id: str | None, source: str
) -> None:
    hub = _hub(doors={3: _door("2")})
    if ours:
        hub.commands.note("door", 3)
    lock = SpcEdpDoorLock(hub, 3)
    lock.async_write_ha_state = MagicMock()
    lock._handle_door_event(_event("DG", "3", GRANTED))
    assert lock.changed_by is None  # a passage does not change the mode
    lock._handle_door_event(_event("DO", "3", description))
    assert lock.changed_by == changed_by
    assert lock.extra_state_attributes["changed_by_id"] == changed_by_id
    assert lock.extra_state_attributes["source"] == source
    lock.async_write_ha_state.assert_called_once()


def test_attribution_window_expires() -> None:
    tracker = CommandTracker(window=0)
    tracker.note("door", 3)
    event = _event("DO", "3", "Entredörr¦admin¦9998¦0")
    with patch("custom_components.spc_edp.attribution.time.monotonic", return_value=1e9):
        assert tracker.attribute("door", 3, event).source == "remote"
    assert tracker.attribute("door", 4, event).source == "remote"


def test_access_attributes_from_captured_events() -> None:
    assert access_attributes(_event("DG", "3", GRANTED)) == {
        "door": "Entredörr",
        "user": "Alice",
        "user_id": "1",
        "card": "123456",
        "direction": "ENTRY",
    }
    assert access_attributes(_event("DD", "3", "Entredörr¦¦0¦0¦ENTRY¦4")) == {
        "door": "Entredörr",
        "direction": "ENTRY",
        "reason_code": "4",
        "reason": "invalid_pin",
    }
    assert access_attributes(_event("DD", "3", "Entredörr¦¦0¦0¦ENTRY¦7"))["reason"] == (
        "invalid_card_format"
    )
    assert access_attributes(_event("DD", "3", "Entredörr¦¦0¦0¦ENTRY¦99"))["reason"] == "unknown"


@pytest.mark.parametrize(
    ("code", "event_type"),
    [
        ("DG", "access_granted"),
        ("DD", "access_denied"),
        ("DI", "access_denied_passback"),
        ("DX", "request_to_exit"),
        ("DF", "door_forced"),
        ("DN", "door_left_open"),
        ("DR", "door_restoral"),
        ("RL", "reader_locked"),
        ("RG", "reader_unlocked"),
    ],
)
def test_access_event_entity_fires_each_door_event(code: str, event_type: str) -> None:
    entity = SpcEdpDoorAccessEvent(_hub(doors={3: _door("0")}), 3)
    entity.async_write_ha_state = MagicMock()
    with patch.object(entity, "_trigger_event") as trigger:
        entity._handle_access(_event(code, "3", "Entredörr"))
        entity._handle_access(_event("DO", "3", "Entredörr¦admin¦9998¦0"))  # a mode change
    trigger.assert_called_once()
    assert trigger.call_args.args[0] == event_type
    assert entity.translation_placeholders == {"door": "Entredörr"}


def test_access_granted_tells_home_assistant_apart() -> None:
    hub = _hub(doors={3: _door("0")})
    entity = SpcEdpDoorAccessEvent(hub, 3)
    entity.async_write_ha_state = MagicMock()
    with patch.object(entity, "_trigger_event") as trigger:
        entity._handle_access(_event("DG", "3", GRANTED))
        hub.commands.note("door", 3)
        entity._handle_access(_event("DG", "3", "Entredörr¦admin¦9998¦0¦ENTRY"))
    first, second = (c.args[1] for c in trigger.call_args_list)
    assert (first["user"], first["source"]) == ("Alice", "panel")
    assert (second["user"], second["source"]) == ("Home Assistant", "home_assistant")


def test_door_alarm_follows_forced_and_left_open_until_restored() -> None:
    sensor = SpcEdpDoorAlarmBinarySensor(_hub(doors={3: _door("0")}), 3)
    sensor.async_write_ha_state = MagicMock()
    assert sensor.is_on is False
    assert sensor.translation_placeholders == {"door": "Entredörr"}
    sensor._handle_door_event(_event("DN", "3", "Entredörr"))
    assert sensor.is_on is True
    assert sensor.extra_state_attributes == {"condition": "door_left_open"}
    sensor._handle_door_event(_event("DG", "3", GRANTED))
    assert sensor.is_on is True
    sensor._handle_door_event(_event("DR", "3", "Entredörr"))
    assert sensor.is_on is False
    sensor._handle_door_event(_event("DF", "3", "Entredörr"))
    assert sensor.extra_state_attributes == {"condition": "door_forced"}
    assert sensor.async_write_ha_state.call_count == 3


@pytest.mark.parametrize(
    ("status", "on"),
    [("0", False), ("1", True), ("2", True), ("4", False), ("5", False), ("7", False), ("", None)],
)
def test_isolate_switch_reports_any_bypass(status: str, on: bool | None) -> None:
    zone = Zone(id=9, type="0", name="Tvättstuga Dörr", area_id=1, input="0", status=status)
    switch = SpcEdpZoneIsolateSwitch(_hub(zones={9: zone}), 9)
    assert switch.is_on is on
    assert switch.translation_placeholders == {"zone": "Tvättstuga Dörr"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "restore"), [("1", "deinhibit"), ("2", "deisolate")])
async def test_isolate_switch_restores_the_reported_bypass(status: str, restore: str) -> None:
    zone = Zone(id=9, type="0", name="Tvättstuga Dörr", area_id=1, input="0", status=status)
    hub = _hub(zones={9: zone})
    control = hub.panel.zone.return_value
    control.isolate = AsyncMock()
    control.deinhibit = AsyncMock()
    control.deisolate = AsyncMock()
    switch = SpcEdpZoneIsolateSwitch(hub, 9)

    await switch.async_turn_off()
    await switch.async_turn_on()

    getattr(control, restore).assert_awaited_once()
    control.isolate.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolate_switch_isolates_a_normal_zone() -> None:
    zone = Zone(id=9, type="0", name="Tvättstuga Dörr", area_id=1, input="0", status="0")
    hub = _hub(zones={9: zone})
    control = hub.panel.zone.return_value
    control.isolate = AsyncMock()
    control.deisolate = AsyncMock()
    switch = SpcEdpZoneIsolateSwitch(hub, 9)

    await switch.async_turn_off()  # already restored: the panel would reject it
    await switch.async_turn_on()

    control.deisolate.assert_not_awaited()
    control.isolate.assert_awaited_once()
    hub.async_refresh.assert_awaited_once()


def _real_hub(hass) -> SpcEdpHub:
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.data = {}
    entry.options = {}
    return SpcEdpHub(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["DG", "DO", "DC", "DF", "DN", "RL"])
async def test_door_events_reach_the_door_entities(hass, code: str) -> None:
    hub = _real_hub(hass)
    panel = MagicMock()
    panel.reconcile_event = AsyncMock(return_value=EventStateUpdate(door_ids=frozenset({3})))

    with patch("custom_components.spc_edp.hub.async_dispatcher_send") as dispatcher_send:
        await hub._handle_sia_event(panel, _event(code, "3", GRANTED))

    signals = [call.args[1] for call in dispatcher_send.call_args_list]
    assert SIGNAL_UPDATE_DOOR.format("test-entry", 3) in signals
    assert SIGNAL_DOOR_ACCESS.format("test-entry", 3) in signals


@pytest.mark.asyncio
async def test_resent_events_are_ignored_after_a_redial(hass) -> None:
    hub = _real_hub(hass)
    panel = MagicMock()
    panel.reconcile_event = AsyncMock(return_value=EventStateUpdate(door_ids=frozenset({3})))
    mode_change = "Entredörr¦Alice¦1¦123456"
    with patch("custom_components.spc_edp.hub.async_dispatcher_send") as dispatcher_send:
        await hub._handle_sia_event(panel, _event("DO", "3", mode_change))
        await hub._handle_sia_event(panel, _event("DO", "3", mode_change))
        await hub._handle_sia_event(panel, _event("DO", "3", mode_change, at="16481103112026"))
        await hub._handle_sia_event(panel, _event("CG", "1", "Huset¦Alice¦1"))
        await hub._handle_sia_event(panel, _event("CG", "1", "Huset¦Alice¦1"))
    signals = [c.args[1] for c in dispatcher_send.call_args_list]
    # The re-sent copy is dropped; a later change with the same text is not.
    assert signals.count(SIGNAL_DOOR_ACCESS.format("test-entry", 3)) == 2
    assert signals.count(SIGNAL_AREA_CHANGE.format("test-entry", 1)) == 1
    assert panel.reconcile_event.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["4", "5", "7"])
async def test_isolate_switch_leaves_tamper_alarm_and_trouble_alone(status: str) -> None:
    zone = Zone(id=9, type="0", name="Tvättstuga Dörr", area_id=1, input="0", status=status)
    hub = _hub(zones={9: zone})
    control = hub.panel.zone.return_value
    control.deinhibit = AsyncMock()
    control.deisolate = AsyncMock()

    await SpcEdpZoneIsolateSwitch(hub, 9).async_turn_off()

    control.deinhibit.assert_not_awaited()
    control.deisolate.assert_not_awaited()
