"""Availability, reconnects and alarm commands."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from spcedp import Area, Panel, SpcTimeout, Zone

from custom_components.spc_edp.alarm_control_panel import SpcEdpAlarmControlPanel
from custom_components.spc_edp.binary_sensor import SpcEdpZoneBinarySensor
from custom_components.spc_edp.const import SIGNAL_UPDATE_AREA, SIGNAL_UPDATE_ZONE
from custom_components.spc_edp.hub import SpcEdpHub


def make_hub(hass):
    entry = MagicMock()
    entry.data = {"bind": "127.0.0.1", "port": 50000, "receiver_id": 1001}
    entry.options = {}
    entry.entry_id = "test-entry"
    entry.unique_id = "test-panel"
    return SpcEdpHub(hass, entry)


async def test_initial_read_failure_releases_session_for_retry(hass):
    hub = make_hub(hass)
    session = MagicMock()
    with patch(
        "custom_components.spc_edp.hub.Panel.from_session",
        side_effect=SpcTimeout("timeout"),
    ):
        await hub._on_session(session)
    assert not hub.available
    assert hub._session is None
    session.request_teardown.assert_called_once()

    panel = Panel(MagicMock(xml_command=AsyncMock(return_value={})))
    connected = asyncio.Event()

    async def events():
        connected.set()
        await asyncio.Future()
        yield

    panel.events = events
    with patch("custom_components.spc_edp.hub.Panel.from_session", return_value=panel):
        task = asyncio.create_task(hub._on_session(MagicMock()))
        await connected.wait()
        assert hub.available
        await hub._on_session(session)  # duplicate cannot replace the active panel
        assert hub.panel is panel
        assert hub.available
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # A dropped link keeps the last state for the reconnect grace period.
    assert not hub.connected
    assert hub.available
    assert hub._session is None
    assert hub._area_unsub is None
    await hub.async_stop()
    assert hub._grace_unsub is None


def _panel():
    return Panel(MagicMock(xml_command=AsyncMock(return_value={})))


async def _connect(hub, panel):
    connected = asyncio.Event()

    async def events():
        connected.set()
        await asyncio.Future()
        yield

    panel.events = events
    with patch("custom_components.spc_edp.hub.Panel.from_session", return_value=panel):
        task = asyncio.create_task(hub._on_session(MagicMock()))
        await connected.wait()
    return task


async def _drop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_redial_within_grace_never_reports_unavailable(hass):
    hub = make_hub(hass)
    with patch("custom_components.spc_edp.hub.async_dispatcher_send") as dispatch:
        await _drop(await _connect(hub, _panel()))
        assert hub.available and not hub.connected
        with patch(
            "custom_components.spc_edp.hub.Panel.from_session",
            side_effect=SpcTimeout("timeout"),
        ):
            await hub._on_session(MagicMock())  # a failed redial keeps the grace running
        assert hub.available
        task = await _connect(hub, _panel())
        assert hub.connected and hub._grace_unsub is None
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=120))
        await hass.async_block_till_done()
        assert hub.available
        await _drop(task)
    await hub.async_stop()
    availability = [
        c.args[2] for c in dispatch.call_args_list if c.args[1] == "spc_edp_availability_test-entry"
    ]
    assert False not in availability


async def test_entities_become_unavailable_after_the_grace_period(hass):
    hub = make_hub(hass)
    await _drop(await _connect(hub, _panel()))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=59))
    await hass.async_block_till_done()
    assert hub.available
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert not hub.available


async def test_zero_grace_marks_entities_unavailable_at_once(hass):
    hub = make_hub(hass)
    hub.entry.options = {"reconnect_grace": 0}
    await _drop(await _connect(hub, _panel()))
    assert not hub.available
    assert hub._grace_unsub is None


async def test_removed_entities_become_unavailable_and_are_notified(hass):
    hub = make_hub(hass)
    session = MagicMock()
    session.xml_command = AsyncMock(return_value={})
    hub.panel = Panel(session)
    hub.panel.areas[1] = Area(id=1, mode="0")
    hub.panel.zones[2] = Zone.from_row({"ID": "2", "INPUT": "0"})
    hub.available = True
    hub.connected = True
    alarm = SpcEdpAlarmControlPanel(hub, 1)
    sensor = SpcEdpZoneBinarySensor(hub, 2)
    assert alarm.available and sensor.available
    with patch("custom_components.spc_edp.hub.async_dispatcher_send") as dispatch:
        await hub.async_refresh()
    assert not alarm.available and not sensor.available
    dispatch.assert_any_call(hass, SIGNAL_UPDATE_AREA.format("test-entry", 1))
    dispatch.assert_any_call(hass, SIGNAL_UPDATE_ZONE.format("test-entry", 2))


@pytest.mark.parametrize(
    "method,opcode",
    [
        ("async_alarm_disarm", "AREA_UNSET"),
        ("async_alarm_arm_home", "AREA_SET_A"),
        ("async_alarm_arm_night", "AREA_SET_B"),
        ("async_alarm_arm_away", "AREA_SET"),
    ],
)
async def test_alarm_controls_refresh_confirmed_state(hass, method, opcode):
    from spcedp.commands import BinaryOp

    hub = make_hub(hass)
    session = MagicMock()
    session.binary_command = AsyncMock()
    session.xml_command = AsyncMock(return_value={"AREA_STATUS": [{"ID": "1", "MODE": "2"}]})
    hub.panel = Panel(session)
    hub.panel.areas[1] = Area(id=1, mode="0")
    hub.available = True
    hub.connected = True
    alarm = SpcEdpAlarmControlPanel(hub, 1)
    await getattr(alarm, method)()
    session.binary_command.assert_awaited_once_with(getattr(BinaryOp, opcode), 1)
    assert hub.panel.areas[1].mode == "2"


async def test_unavailable_alarm_does_not_send_commands(hass):
    hub = make_hub(hass)
    alarm = SpcEdpAlarmControlPanel(hub, 1)
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await alarm.async_alarm_arm_home()
