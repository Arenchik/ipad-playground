"""Base entities for the TWRA Aroma Diffuser."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import TwraConfigEntry
from .const import DOMAIN
from .twra import TwraClient, WorkingMode


class TwraEntity(Entity):
    """Pushes state whenever the client reports a change."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: TwraConfigEntry, key: str) -> None:
        self._client: TwraClient = entry.runtime_data.client
        address = self._client.address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=entry.title,
            manufacturer="Tersway",
            model="TW-RA01 Smart Tower",
            serial_number=self._client.identity.serial,
        )

    @property
    def available(self) -> bool:
        # In on-demand mode the link is closed between commands, so the
        # last known state stays valid.
        if self._client.keep_connected:
            return self._client.connected
        return self._client.state.power is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.register_callback(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class TwraModeEntity(TwraEntity):
    """An entity bound to one working mode (schedule record)."""

    def __init__(self, entry: TwraConfigEntry, serial: int, key: str) -> None:
        super().__init__(entry, f"mode{serial}_{key}")
        self._serial = serial
        self._attr_translation_placeholders = {"mode": str(serial)}

    @property
    def mode(self) -> WorkingMode | None:
        return next((m for m in self._client.state.modes if m.serial == self._serial), None)

    @property
    def available(self) -> bool:
        return super().available and self.mode is not None
