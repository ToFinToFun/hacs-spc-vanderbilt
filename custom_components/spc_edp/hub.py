"""Own one panel connection and publish sensor, alarm-area and door updates."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from spcedp import Panel, PanelServer, Session, SiaEvent, SpcError

from .const import (
    CONF_AREA_REFRESH_INTERVAL,
    CONF_BIND,
    CONF_ENCRYPTION_KEY,
    CONF_IDLE_TIMEOUT,
    CONF_PORT,
    CONF_RECEIVER_ID,
    CONF_RECONNECT_GRACE,
    DEFAULT_AREA_REFRESH_INTERVAL,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_RECONNECT_GRACE,
    DOOR_SIA_CODES,
    SIGNAL_AVAILABILITY,
    SIGNAL_DOOR_ACCESS,
    SIGNAL_NEW_AREAS,
    SIGNAL_NEW_DOORS,
    SIGNAL_NEW_ZONES,
    SIGNAL_UPDATE_AREA,
    SIGNAL_UPDATE_DOOR,
    SIGNAL_UPDATE_ZONE,
)

_LOGGER = logging.getLogger(__name__)


class SpcEdpHub:
    """Owns the PanelServer/Panel lifecycle for a single config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[SpcEdpHub]) -> None:
        """Initialize the hub (does not start listening yet)."""
        self.hass = hass
        self.entry = entry
        self.panel: Panel | None = None
        # `connected` follows the live link; `available` is what entities show
        # and outlasts a short redial by the reconnect grace period.
        self.connected = False
        self.available = False
        self._server: PanelServer | None = None
        self._area_unsub: Callable[[], None] | None = None
        self._grace_unsub: CALLBACK_TYPE | None = None
        self._stopping = False
        self._refresh_lock = asyncio.Lock()

        self._session: Session | None = None

    @property
    def receiver_id(self) -> int:
        """The EDP receiver id the panel is configured to dial."""
        return cast(int, self.entry.data[CONF_RECEIVER_ID])

    @property
    def bind(self) -> str:
        """The local address the listen socket is bound to."""
        return cast(str, self.entry.data[CONF_BIND])

    @property
    def port(self) -> int:
        """The local TCP port the listen socket is bound to."""
        return cast(int, self.entry.data[CONF_PORT])

    @property
    def encryption_key(self) -> str | None:
        """The 32-hex-digit EDP AES key, or None if encryption is disabled."""
        return self.entry.data.get(CONF_ENCRYPTION_KEY)

    @property
    def idle_timeout(self) -> float:
        """Seconds of silence before a stale panel connection is dropped."""
        return float(self.entry.options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))

    @property
    def area_refresh_interval(self) -> float:
        """Poll interval (seconds) for the area/zone drift-reconciliation pass."""
        return float(
            self.entry.options.get(CONF_AREA_REFRESH_INTERVAL, DEFAULT_AREA_REFRESH_INTERVAL)
        )

    @property
    def reconnect_grace(self) -> float:
        """Seconds entities keep their last state while the panel redials."""
        return float(self.entry.options.get(CONF_RECONNECT_GRACE, DEFAULT_RECONNECT_GRACE))

    @property
    def unique_id(self) -> str:
        """Stable identity for this configured panel."""
        return self.entry.unique_id or self.entry.entry_id

    async def async_start(self) -> None:
        """Open the EDP listen socket. Raises ConfigEntryNotReady on failure."""
        self._server = PanelServer(
            receiver_id=self.receiver_id,
            bind=self.bind,
            port=self.port,
            key=self.encryption_key,
            idle_timeout=self.idle_timeout,
            on_session=self._on_session,
        )
        try:
            # The listener context spans Home Assistant setup and unload.
            await self._server.__aenter__()  # pylint: disable=unnecessary-dunder-call
        except OSError as err:
            self._server = None
            raise ConfigEntryNotReady(
                f"Could not bind EDP receiver on {self.bind}:{self.port}: {err}"
            ) from err

    async def async_stop(self) -> None:
        """Close the listener and its connections."""
        self._stopping = True
        self._stop_area_refresh()
        if self._server is not None:
            await self._server.__aexit__(None, None, None)
            self._server = None
        self._cancel_grace()

    async def _on_session(self, session: Session) -> None:
        """Keep one connected panel, allowing retry after any setup failure."""
        if self._session is not None:
            _LOGGER.warning("Rejecting a duplicate panel connection")
            session.request_teardown()
            return
        self._session = session
        try:
            self.panel = await Panel.from_session(session)
            await self._refresh_doors(self.panel)
            self._cancel_grace()
            self.connected = True
            self.available = True
            self._announce_new_entities()
            self._async_set_availability(True)
            self._start_area_refresh()
            async for event in self.panel.events():
                await self._handle_sia_event(self.panel, event)
        except SpcError:
            _LOGGER.warning("Panel communication failed", exc_info=True)
        finally:
            session.request_teardown()
            self._session = None
            was_connected = self.connected
            self.connected = False
            self._stop_area_refresh()
            if self._stopping:
                self._async_mark_unavailable()
            elif was_connected and self.available and self.reconnect_grace > 0:
                self._grace_unsub = async_call_later(
                    self.hass, self.reconnect_grace, self._async_grace_expired
                )
            elif self._grace_unsub is None:
                # A failed redial inside the grace period leaves its timer running.
                self._async_mark_unavailable()

    def _async_set_availability(self, available: bool) -> None:
        async_dispatcher_send(self.hass, SIGNAL_AVAILABILITY.format(self.entry.entry_id), available)

    @callback
    def _async_grace_expired(self, _now: object) -> None:
        """The panel did not redial in time; show its entities as unavailable."""
        self._grace_unsub = None
        if not self.connected:
            _LOGGER.warning(
                "SPC panel has not reconnected within %ss; marking entities unavailable",
                self.reconnect_grace,
            )
            self._async_mark_unavailable()

    def _async_mark_unavailable(self) -> None:
        self._cancel_grace()
        self.available = False
        self._async_set_availability(False)

    def _cancel_grace(self) -> None:
        if self._grace_unsub is not None:
            self._grace_unsub()
            self._grace_unsub = None

    def _announce_new_entities(self) -> None:
        """Platforms deduplicate IDs when adding entities."""
        if self.panel is None:
            return
        entry_id = self.entry.entry_id
        async_dispatcher_send(self.hass, SIGNAL_NEW_AREAS.format(entry_id), set(self.panel.areas))
        async_dispatcher_send(self.hass, SIGNAL_NEW_ZONES.format(entry_id), set(self.panel.zones))
        async_dispatcher_send(self.hass, SIGNAL_NEW_DOORS.format(entry_id), set(self.panel.doors))

    @staticmethod
    async def _refresh_doors(panel: Panel) -> None:
        """Read doors; panels without access control simply report none."""
        try:
            await panel.refresh_doors()
        except SpcError:
            _LOGGER.debug("Door status refresh failed", exc_info=True)

    async def _handle_sia_event(self, panel: Panel, event: SiaEvent) -> None:
        """Publish zone events and authoritative area-state reads."""
        _LOGGER.debug(
            "SIA event %s address=%s: %s", event.sia_code, event.address, event.description
        )
        try:
            async with self._refresh_lock:
                update = await panel.reconcile_event(event)
        except SpcError:
            _LOGGER.warning(
                "Immediate state reconciliation failed for SIA code %s; "
                "the periodic refresh will retry",
                event.sia_code,
                exc_info=True,
            )
            update = panel.apply_event(event)

        _LOGGER.debug("SIA event %s updated %s", event.sia_code, update)
        self._announce_new_entities()
        entry_id = self.entry.entry_id
        for zone_id in update.zone_ids:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE_ZONE.format(entry_id, zone_id))
        for area_id in update.area_ids:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE_AREA.format(entry_id, area_id))
        for door_id in update.door_ids:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE_DOOR.format(entry_id, door_id))
        address = event.address.strip()
        if event.sia_code.upper() in DOOR_SIA_CODES and address.isdigit():
            async_dispatcher_send(
                self.hass, SIGNAL_DOOR_ACCESS.format(entry_id, int(address)), event
            )

    def _start_area_refresh(self) -> None:
        self._stop_area_refresh()
        self._area_unsub = async_track_time_interval(
            self.hass,
            self.async_refresh,
            timedelta(seconds=self.area_refresh_interval),
        )

    def _stop_area_refresh(self) -> None:
        if self._area_unsub is not None:
            self._area_unsub()
            self._area_unsub = None

    async def async_refresh(self, _now: object = None) -> None:
        """Reconcile state and notify removed entities as well as current ones."""
        panel = self.panel
        if panel is None or not self.connected:
            return
        if _now is not None and self._refresh_lock.locked():
            return
        try:
            async with self._refresh_lock:
                if panel is not self.panel or not self.connected:
                    return
                area_ids = set(panel.areas)
                zone_ids = set(panel.zones)
                door_ids = set(panel.doors)
                await panel.refresh_areas()
                await panel.refresh_zones()
                await self._refresh_doors(panel)
        except SpcError:
            _LOGGER.debug("Area/zone reconciliation refresh failed", exc_info=True)
        if panel is not self.panel or not self.connected:
            return
        self._announce_new_entities()
        entry_id = self.entry.entry_id
        for area_id in area_ids | set(panel.areas):
            async_dispatcher_send(self.hass, SIGNAL_UPDATE_AREA.format(entry_id, area_id))
        for zone_id in zone_ids | set(panel.zones):
            async_dispatcher_send(self.hass, SIGNAL_UPDATE_ZONE.format(entry_id, zone_id))
        for door_id in door_ids | set(panel.doors):
            async_dispatcher_send(self.hass, SIGNAL_UPDATE_DOOR.format(entry_id, door_id))
