"""Door modes shared by the lock and door mode select entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from spcedp import Door, DoorMode

from .commands import async_run_command

if TYPE_CHECKING:
    from .hub import SpcEdpHub

# The panel's door modes as Home Assistant shows them. "Unlocked" is the
# panel's open mode: the lock stays released until the mode changes, whether
# or not the door itself is opened (that is the door's position zone).
DOOR_MODE_OPTIONS: dict[DoorMode, str] = {
    DoorMode.LOCKED: "locked",
    DoorMode.NORMAL: "normal",
    DoorMode.OPEN: "unlocked",
}


def door_mode_option(door: Door | None) -> str | None:
    """The door's mode as `locked`, `normal` or `unlocked`."""
    mode = door.door_mode if door else None
    return DOOR_MODE_OPTIONS.get(mode) if mode else None


async def async_set_door_mode(hub: SpcEdpHub, door_id: int, option: str) -> None:
    """Set a door to `locked`, `normal` or `unlocked` unless it already is.

    Callers check that the panel is connected first.
    """
    panel = hub.panel
    if panel is None:
        return
    if door_mode_option(panel.doors.get(door_id)) == option:
        return  # the panel rejects setting a door to its current mode
    control = panel.door(door_id)
    command, action = {
        "locked": (control.lock, "setting this door to locked"),
        "normal": (control.set_normal, "setting this door to normal"),
        "unlocked": (control.open_permanent, "unlocking this door"),
    }[option]
    hub.commands.note("door", door_id)
    await async_run_command(command(), action)
    await hub.async_refresh()
