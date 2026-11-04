"""Who caused a panel event, and whether Home Assistant sent the command."""

from __future__ import annotations

import time
from dataclasses import dataclass

from spcedp import SiaEvent

from .const import (
    COMMAND_ATTRIBUTION_WINDOW,
    EDP_USER_ID,
    SOURCE_HOME_ASSISTANT,
    SOURCE_PANEL,
    SOURCE_PANEL_AUTOMATIC,
    SOURCE_REMOTE,
)

# Door and area event descriptions start "<name>¦<user>¦<user id>¦...".
_SEPARATOR = "¦"


@dataclass(frozen=True, slots=True)
class Attribution:
    """The user the panel named for an event and where the action came from."""

    changed_by: str | None
    changed_by_id: str | None
    source: str

    def as_attributes(self) -> dict[str, str | None]:
        """State attributes; HA's own changed_by is set separately."""
        return {"changed_by_id": self.changed_by_id, "source": self.source}


def event_user(event: SiaEvent) -> tuple[str | None, str | None]:
    """Return the (name, id) the description names, if any."""
    fields = event.description.split(_SEPARATOR)
    name = fields[1].strip() if len(fields) > 1 else ""
    user_id = fields[2].strip() if len(fields) > 2 else ""
    if not user_id.isdigit() or user_id == "0":
        return (name or None, None)
    return (name or None, user_id)


class CommandTracker:
    """Remember recent Home Assistant commands so their events can be attributed."""

    def __init__(self, window: float = COMMAND_ATTRIBUTION_WINDOW) -> None:
        """Initialize an empty tracker."""
        self._window = window
        self._sent: dict[tuple[str, int], float] = {}

    def note(self, kind: str, target_id: int) -> None:
        """Record that Home Assistant is about to command this door or area."""
        self._sent[(kind, target_id)] = time.monotonic()

    def attribute(self, kind: str, target_id: int, event: SiaEvent) -> Attribution:
        """Attribute an event to Home Assistant, a panel user or the panel itself."""
        name, user_id = event_user(event)
        if user_id == EDP_USER_ID:
            sent = self._sent.get((kind, target_id))
            if sent is not None and time.monotonic() - sent <= self._window:
                return Attribution("Home Assistant", user_id, SOURCE_HOME_ASSISTANT)
            return Attribution(name, user_id, SOURCE_REMOTE)
        if user_id is not None:
            return Attribution(name, user_id, SOURCE_PANEL)
        return Attribution(None, None, SOURCE_PANEL_AUTOMATIC)
