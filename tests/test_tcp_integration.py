"""Exercise real HA setup, services, entities and reloads over the EDP socket."""

import asyncio
import re

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from spcedp.commands import BinaryOp
from spcedp.wire import Frame, FrameDecoder

from custom_components.spc_edp.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

DOOR_MODES = {
    BinaryOp.DOOR_SET_NORMAL: "0",
    BinaryOp.DOOR_LOCK: "1",
    BinaryOp.DOOR_OPEN_PERMANENT: "2",
}


class SimulatedPanel:
    """Two areas, two sensors and one door, with the event bursts observed on real hardware."""

    def __init__(self, port, key):
        self.port, self.key = port, key
        self.modes = {1: "0", 2: "0"}
        self.door_mode = "0"
        self.gate = "0"
        self.clock = 0
        self.open = False
        self.sequence = 0
        self.writer = None
        self.task = None

    async def connect(self):
        self.sequence = 0
        reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.task = asyncio.create_task(self.serve(reader))
        self.send(1, 2, b"12345678")
        self.send(1, 0, b"12345678")

    def send(self, major, minor, payload, sequence=None):
        self.sequence = max(self.sequence + (sequence is None), sequence or 0)
        self.writer.write(
            Frame(
                src_id=1000,
                dst_id=1001,
                sequence=sequence or self.sequence,
                major=major,
                minor=minor,
                payload=payload,
            ).encode(key=self.key)
        )

    async def serve(self, reader):
        decoder = FrameDecoder(key=self.key)
        while data := await reader.read(4096):
            for frame in decoder.feed(data):
                if frame.major not in (4, 10):
                    continue
                assert frame.sequence > self.sequence, "Command collided with a panel event"
                if frame.major == 10:
                    command = re.search(rb'ID="([a-z_]+)"', frame.payload)[1]
                    rows = {
                        b"info": '<INFO TYPE="SPC4000" VARIANT="4300" '
                        'SN="000003e8" VERSION="3.15.0"/>',
                        b"area_status": "<AREA_STATUS>"
                        + "".join(
                            f'<AREA ID="{n}" NAME="Area {n}" MODE="{mode}"/>'
                            for n, mode in self.modes.items()
                        )
                        + "</AREA_STATUS>",
                        b"zone_status": "<ZONE_STATUS>"
                        f'<ZONE ID="1" ZONE_NAME="Front Door" AREA="1" '
                        f'INPUT="{int(self.open)}" TYPE="1"/>'
                        '<ZONE ID="2" ZONE_NAME="Garage PIR" AREA="2" INPUT="0" TYPE="0"/>'
                        "</ZONE_STATUS>",
                        b"output_status": "<OUTPUT_STATUS>"
                        f'<OUTPUT ID="1" NAME="Front Door unlocked" STATE="{self.gate_state}"/>'
                        "</OUTPUT_STATUS>",
                        b"status": '<SYSINFO TIME="12331920092026" ENGMODE="0"/>'
                        '<PSU BATT_VOLT="13.5V" AUX_VOLT="13.5V" AUX_CURR="300mA" AC_FREQ="50Hz"/>'
                        '<SYSALERT INPUT="00000000" ALERT="00000000" INHIBIT="00000000" '
                        'ISOLATE="00000000"/>',
                        b"door_status": "<DOOR_STATUS>"
                        f'<DOOR ID="1" ZONE="1" ZONE_NAME="Front Door" AREA="1" '
                        f'MODE="{self.door_mode}" STATUS="0" DPS_INPUT="0" DRS_INPUT="0"/>'
                        "</DOOR_STATUS>",
                    }
                    self.send(
                        10,
                        1,
                        b"\x01<COMMAND_REPLY>" + rows[command].encode() + b"</COMMAND_REPLY>",
                        frame.sequence,
                    )
                elif frame.payload[0] in DOOR_MODES:
                    opcode, door, _ = frame.payload
                    self.door_mode = DOOR_MODES[opcode]
                    self.send(4, 2, b"\xf0", frame.sequence)
                    code = "DC" if opcode == BinaryOp.DOOR_LOCK else "DO"
                    self.event(code, door, "Front Door¦admin¦9998¦0")
                else:
                    opcode, area, _ = frame.payload
                    self.modes[area] = {
                        BinaryOp.AREA_UNSET: "0",
                        BinaryOp.AREA_SET_A: "1",
                        BinaryOp.AREA_SET_B: "2",
                        BinaryOp.AREA_SET: "3",
                    }[opcode]
                    self.send(4, 2, b"\xf0", frame.sequence)
                    code = "OG" if opcode == BinaryOp.AREA_UNSET else "NL"
                    self.event(code, area, f"Area {area}¦Remote Engineer¦9998")
                    await asyncio.sleep(0.01)
                    self.event("OQ", 9998)

    def event(self, code, address, description="Test"):
        # A distinct panel timestamp per event, as on hardware a second apart.
        self.clock += 1
        stamp = f"1233{self.clock % 60:02d}20092026"
        payload = f"E2[#1000|{stamp}|{code}|{address}|{description}||0]"
        self.send(2, 0, payload.encode("latin-1"))

    @property
    def gate_state(self):
        """A mapping gate following the lock output: on while held unlocked or released."""
        return "1" if self.gate == "1" or self.door_mode == "2" else "0"

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def wait_for(predicate):
    """Wait for HA's observable state, which has no single completion event."""
    async with asyncio.timeout(10):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("key", [None, bytes.fromhex("00112233445566778899aabbccddeeff")])
async def test_setup_services_reload_and_reconnect(hass, unused_tcp_port, key):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        title="Test alarm",
        unique_id="test-panel",
        data={
            "bind": "127.0.0.1",
            "port": unused_tcp_port,
            "receiver_id": 1001,
            "encryption_key": key.hex() if key else None,
        },
    )
    entry.add_to_hass(hass)
    panel = SimulatedPanel(unused_tcp_port, key)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    try:
        await panel.connect()
        await wait_for(
            lambda: len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 40
        )
        entities = {
            e.unique_id: e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        alarm = entities["test-panel-area-1"]
        other = entities["test-panel-area-2"]
        sensor = entities["test-panel-zone-1"]
        lock = entities["test-panel-door-1"]
        mode = entities["test-panel-door-1-mode"]
        lock_output = entities["test-panel-door-1-lock-output"]
        gate = entities["test-panel-output-1"]
        panel_event = entities["test-panel-panel-event"]
        assert {
            "test-panel-door-1-alarm",
            "test-panel-door-1-exit-button",
            "test-panel-zone-1-fault",
            "test-panel-zone-1-tamper",
            "test-panel-zone-1-status",
            "test-panel-zone-1-input",
            "test-panel-engineer-mode",
            "test-panel-mains-fault",
            "test-panel-battery-voltage",
        } <= set(entities)
        await wait_for(
            lambda: hass.states.get(alarm) and hass.states.get(alarm).state == "disarmed"
        )
        changes = []
        unsub = hass.bus.async_listen("state_changed", lambda ev: changes.append(ev.data))
        for service, expected in [
            ("alarm_arm_home", "armed_home"),
            ("alarm_disarm", "disarmed"),
            ("alarm_arm_night", "armed_night"),
            ("alarm_disarm", "disarmed"),
            ("alarm_arm_away", "armed_away"),
            ("alarm_disarm", "disarmed"),
        ]:
            await hass.services.async_call(
                "alarm_control_panel", service, {"entity_id": alarm}, blocking=True
            )
            assert hass.states.get(alarm).state == expected
            assert hass.states.get(other).state == "disarmed"
        await wait_for(
            lambda: hass.states.get(alarm).attributes.get("changed_by") == "Home Assistant"
        )
        assert hass.states.get(alarm).attributes["source"] == "home_assistant"
        assert hass.states.get(lock).state == "locked"
        await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
        assert hass.states.get(lock).state == "unlocked"
        assert hass.states.get(mode).state == "unlocked"
        await wait_for(
            lambda: hass.states.get(lock).attributes.get("changed_by") == "Home Assistant"
        )
        assert hass.states.get(lock).attributes["changed_by_id"] == "9998"
        assert hass.states.get(lock).attributes["source"] == "home_assistant"
        await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
        assert hass.states.get(lock).state == "locked"
        assert hass.states.get(mode).state == "normal"
        await hass.services.async_call(
            "select", "select_option", {"entity_id": mode, "option": "locked"}, blocking=True
        )
        assert hass.states.get(mode).state == "locked"
        assert hass.states.get(lock).state == "locked"
        await hass.services.async_call(
            "select", "select_option", {"entity_id": mode, "option": "normal"}, blocking=True
        )
        assert hass.states.get(mode).state == "normal"
        panel.open = True
        panel.event("ZO", 1)
        await wait_for(lambda: hass.states.get(sensor).state == "on")
        assert hass.states.get(panel_event).attributes["event_type"] == "zo"
        assert hass.states.get(panel_event).attributes["zone"] == "Front Door"
        assert hass.states.get(mode).name == "SPC4300 Front Door mode"
        # A mapping-gate output that follows the lock makes a momentary open visible.
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": lock_output, "option": "Front Door unlocked"},
            blocking=True,
        )
        assert hass.states.get(lock).state == "locked"
        panel.gate = "1"
        await wait_for(lambda: hass.states.get(gate).state == "on")
        await wait_for(lambda: hass.states.get(lock).state == "unlocked")
        assert hass.states.get(lock).attributes["lock_output"] == "Front Door unlocked"
        panel.gate = "0"
        await wait_for(lambda: hass.states.get(lock).state == "locked")
        # The panel redials its link several times a day; within the reconnect
        # grace period entities keep their state and commands wait for the link.
        await panel.close()
        await wait_for(lambda: not entry.runtime_data.connected)
        assert hass.states.get(sensor).state == "on"
        with pytest.raises(HomeAssistantError, match="unavailable"):
            await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
        await panel.connect()
        await wait_for(lambda: entry.runtime_data.connected)
        await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
        assert hass.states.get(lock).state == "unlocked"
        assert hass.states.get(sensor).state == "on"
        assert not any(
            e.get("new_state") and e["new_state"].state == "unavailable" for e in changes
        )
        unsub()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await panel.close()
        await panel.connect()
        await wait_for(lambda: hass.states.get(alarm).state == "disarmed")
        assert {
            e.unique_id: e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        } == entities
    finally:
        await panel.close()
        hub = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert hub._server is None
