"""Start and end time of each working mode."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TwraConfigEntry, async_run_command, minute_to_time, time_to_minute
from .entity import TwraModeEntity

FIELDS = {"start": "start_minute", "end": "end_minute"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        TwraModeTime(entry, serial, key)
        for serial in sorted(entry.runtime_data.mode_serials)
        for key in FIELDS
    )


class TwraModeTime(TwraModeEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: TwraConfigEntry, serial: int, key: str) -> None:
        super().__init__(entry, serial, key)
        self._field = FIELDS[key]
        self._attr_translation_key = f"mode_{key}"

    @property
    def native_value(self) -> time | None:
        mode = self.mode
        return minute_to_time(getattr(mode, self._field)) if mode else None

    async def async_set_value(self, value: time) -> None:
        if (mode := self.mode) is not None:
            await async_run_command(
                self._client.update_mode(mode.with_changes(**{self._field: time_to_minute(value)}))
            )
