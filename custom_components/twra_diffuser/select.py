"""Spray duration as a select.

HomeKit Bridge does not carry number entities, but it turns a select
into one accessory with a switch per option, so the spray time becomes
a row of labelled buttons in the Home app. The option strings are what
HomeKit shows, hence the localised unit.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TwraConfigEntry, async_run_command
from .const import DOMAIN, SPRAY_TIMES
from .entity import TwraEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    unit = "с" if (hass.config.language or "").startswith("ru") else "s"
    async_add_entities([TwraSprayTime(entry, unit)])


class TwraSprayTime(TwraEntity, SelectEntity):
    _attr_translation_key = "spray_time"
    _attr_icon = "mdi:spray"

    def __init__(self, entry: TwraConfigEntry, unit: str) -> None:
        super().__init__(entry, "spray_time")
        self._seconds = {f"{seconds} {unit}": seconds for seconds in SPRAY_TIMES}
        self._attr_options = list(self._seconds)

    @property
    def current_option(self) -> str | None:
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            return None
        return next(
            (opt for opt, sec in self._seconds.items() if sec == modes[0].run_seconds), None
        )

    async def async_select_option(self, option: str) -> None:
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
        seconds = self._seconds[option]
        for mode in modes:
            if mode.run_seconds != seconds:
                await async_run_command(
                    self._client.update_mode(mode.with_changes(run_seconds=seconds))
                )
