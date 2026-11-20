"""Enable/disable each working mode.

These are configuration entities, which HomeKit Bridge leaves out unless
they are included by hand, so the Home app only shows the diffuser and
its lamp.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TwraConfigEntry, async_run_command, minute_to_time
from .entity import TwraModeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        TwraModeSwitch(entry, serial) for serial in sorted(entry.runtime_data.mode_serials)
    )


class TwraModeSwitch(TwraModeEntity, SwitchEntity):
    _attr_translation_key = "mode_enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: TwraConfigEntry, serial: int) -> None:
        super().__init__(entry, serial, "enabled")

    @property
    def is_on(self) -> bool | None:
        mode = self.mode
        return mode.enabled if mode else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mode = self.mode
        if mode is None:
            return {}
        return {
            "days": mode.days,
            "start": minute_to_time(mode.start_minute).isoformat("minutes"),
            "end": minute_to_time(mode.end_minute).isoformat("minutes"),
            "run_seconds": mode.run_seconds,
            "pause_seconds": mode.pause_seconds,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        if (mode := self.mode) is not None:
            await async_run_command(self._client.update_mode(mode.with_changes(enabled=enabled)))
