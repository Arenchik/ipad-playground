"""Spray and pause durations of each working mode."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TwraConfigEntry, async_run_command
from .entity import TwraModeEntity

FIELDS = {
    # key: (record field, max seconds)
    "run": ("run_seconds", 600),
    "pause": ("pause_seconds", 3600),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        TwraModeNumber(entry, serial, key)
        for serial in sorted(entry.runtime_data.mode_serials)
        for key in FIELDS
    )


class TwraModeNumber(TwraModeEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 1
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: TwraConfigEntry, serial: int, key: str) -> None:
        super().__init__(entry, serial, key)
        self._field, self._attr_native_max_value = FIELDS[key]
        self._attr_translation_key = f"mode_{key}"

    @property
    def native_value(self) -> int | None:
        mode = self.mode
        return getattr(mode, self._field) if mode else None

    async def async_set_native_value(self, value: float) -> None:
        if (mode := self.mode) is not None:
            await async_run_command(
                self._client.update_mode(mode.with_changes(**{self._field: int(value)}))
            )
