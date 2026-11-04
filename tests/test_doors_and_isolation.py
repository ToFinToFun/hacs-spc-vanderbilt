"""Door locks, door access events and zone isolate switches."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from spcedp import Door, Zone
from spcedp.events import SiaEvent
from spcedp.panel import EventStateUpdate

from custom_components.spc_edp.const import SIGNAL_DOOR_ACCESS, SIGNAL_UPDATE_DOOR
from custom_components.spc_edp.event import SpcEdpDoorAccessEvent, access_attributes
from custom_components.spc_edp.hub import SpcEdpHub
from custom_components.spc_edp.lock import SpcEdpDoorLock
from custom_components.spc_edp.switch import SpcEdpZoneIsolateSwitch


def _event(code: str, address: str, description: str) -> SiaEvent:
    return SiaEvent(
        spc_id=4567,
        timestamp=None,
        timestamp_raw="16481003112026",
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
    hub.async_refresh = AsyncMock()
    return hub


@pytest.mark.parametrize(
    ("mode", "locked", "attribute"),
    [("0", True, "normal"), ("1", True, "locked"), ("2", False, "open"), ("9", None, None)],
)
def test_lock_state_follows_door_mode(
    mode: str, locked: bool | None, attribute: str | None
) -> None:
    lock = SpcEdpDoorLock(_hub(doors={3: _door(mode)}), 3)
    assert lock.is_locked is locked
    assert lock.extra_state_attributes == {
        "door_mode": attribute,
        "door_open": False,
        "changed_by_id": None,
    }
    assert lock.name == "Entredörr"


def test_lock_is_unavailable_for_a_missing_door() -> None:
    lock = SpcEdpDoorLock(_hub(), 3)
    assert lock.available is False
    assert lock.is_locked is None


@pytest.mark.asyncio
async def test_lock_commands_map_to_door_controls() -> None:
    hub = _hub(doors={3: _door("1")})
    control = hub.panel.door.return_value
    control.set_normal = AsyncMock()
    control.open_permanent = AsyncMock()
    control.open_momentary = AsyncMock()
    lock = SpcEdpDoorLock(hub, 3)

    await lock.async_unlock()
    await lock.async_lock()
    await lock.async_open()

    hub.panel.door.assert_called_with(3)
    control.open_permanent.assert_awaited_once()
    control.set_normal.assert_awaited_once()
    control.open_momentary.assert_awaited_once()


@pytest.mark.parametrize(("status", "on"), [("0", False), ("1", True), ("2", True), ("", None)])
def test_isolate_switch_reports_any_bypass(status: str, on: bool | None) -> None:
    zone = Zone(id=9, type="0", name="Tvättstuga Dörr", area_id=1, input="0", status=status)
    switch = SpcEdpZoneIsolateSwitch(_hub(zones={9: zone}), 9)
    assert switch.is_on is on
    assert switch.name == "Tvättstuga Dörr isolated"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "skipped"), [("0", "set_normal"), ("2", "open_permanent")])
async def test_lock_skips_commands_for_the_current_mode(mode: str, skipped: str) -> None:
    hub = _hub(doors={3: _door(mode)})
    control = hub.panel.door.return_value
    setattr(control, skipped, AsyncMock())
    lock = SpcEdpDoorLock(hub, 3)

    await (lock.async_lock() if skipped == "set_normal" else lock.async_unlock())

    getattr(control, skipped).assert_not_awaited()
    hub.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_commands_need_a_live_link_during_the_reconnect_grace() -> None:
    hub = _hub(doors={3: _door("1")})
    hub.connected = False
    lock = SpcEdpDoorLock(hub, 3)
    assert lock.available
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await lock.async_unlock()


@pytest.mark.parametrize(
    ("event", "changed_by", "changed_by_id"),
    [
        (("DO", "3", "Entredörr¦admin¦9998¦0"), "admin", "9998"),
        (("DC", "3", "Entredörr¦Alice¦1¦0"), "Alice", "1"),
        (("DO", "3", "Entredörr¦¦0¦0"), None, None),
    ],
)
def test_lock_records_who_changed_the_mode(
    event: tuple[str, str, str], changed_by: str | None, changed_by_id: str | None
) -> None:
    lock = SpcEdpDoorLock(_hub(doors={3: _door("2")}), 3)
    lock.async_write_ha_state = MagicMock()
    lock._handle_door_event(_event("DG", "3", "Entredörr¦Bob¦2¦123456¦ENTRY"))
    assert lock.changed_by is None
    lock._handle_door_event(_event(*event))
    assert lock.changed_by == changed_by
    assert lock.extra_state_attributes["changed_by_id"] == changed_by_id
    lock.async_write_ha_state.assert_called_once()


def test_access_attributes_from_captured_event() -> None:
    granted = _event("DG", "3", "Entredörr¦Alice¦1¦123456¦ENTRY")
    assert access_attributes(granted) == {
        "door": "Entredörr",
        "user": "Alice",
        "user_id": "1",
        "card": "123456",
        "direction": "ENTRY",
    }
    denied = _event("DD", "3", "Entredörr¦¦0¦0¦ENTRY¦4")
    assert access_attributes(denied)["user_id"] == "0"
    assert access_attributes(denied)["reason"] == "4"
    assert "user" not in access_attributes(denied)


def test_access_event_entity_triggers_mapped_type() -> None:
    entity = SpcEdpDoorAccessEvent(_hub(doors={3: _door("0")}), 3)
    entity.async_write_ha_state = MagicMock()
    with patch.object(entity, "_trigger_event") as trigger:
        entity._handle_access(_event("DG", "3", "Entredörr¦Alice¦1¦123456¦ENTRY"))
        entity._handle_access(_event("DO", "3", "Entredörr¦admin¦9998¦0"))
    trigger.assert_called_once()
    assert trigger.call_args.args[0] == "access_granted"
    with patch.object(entity, "_trigger_event") as trigger:
        entity._handle_access(_event("DF", "3", "Entredörr"))
        entity._handle_access(_event("DR", "3", "Entredörr"))
    assert [call.args[0] for call in trigger.call_args_list] == [
        "forced_open",
        "forced_open_restored",
    ]
    assert entity.name == "Entredörr access"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["DG", "DO", "DF"])
async def test_door_event_updates_lock_and_access_entities(hass, code: str) -> None:
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.data = {}
    entry.options = {}
    hub = SpcEdpHub(hass, entry)
    event = _event(code, "3", "Entredörr¦Alice¦1¦123456¦ENTRY")
    panel = MagicMock()
    panel.reconcile_event = AsyncMock(return_value=EventStateUpdate(door_ids=frozenset({3})))

    with patch("custom_components.spc_edp.hub.async_dispatcher_send") as dispatcher_send:
        await hub._handle_sia_event(panel, event)

    signals = [call.args[1] for call in dispatcher_send.call_args_list]
    assert SIGNAL_UPDATE_DOOR.format("test-entry", 3) in signals
    assert SIGNAL_DOOR_ACCESS.format("test-entry", 3) in signals
