"""The diffuser itself, as a fan: power plus scent intensity.

A fan is the HomeKit-friendliest shape for a diffuser: one tile with an
on/off toggle and a speed slider, and Siri understands "set the diffuser
to 40%". Speed maps to the pause between sprays (INTENSITY_PAUSES).
"""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import TwraConfigEntry, async_run_command
from .const import DOMAIN, INTENSITY_PAUSES
from .entity import TwraEntity

SPEED_RANGE = (1, len(INTENSITY_PAUSES))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TwraDiffuserFan(entry)])


class TwraDiffuserFan(TwraEntity, FanEntity):
    _attr_name = None
    _attr_supported_features = (
        FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF | FanEntityFeature.SET_SPEED
    )
    _attr_speed_count = len(INTENSITY_PAUSES)

    def __init__(self, entry: TwraConfigEntry) -> None:
        super().__init__(entry, "diffuser")

    @property
    def is_on(self) -> bool | None:
        return self._client.state.power

    @property
    def percentage(self) -> int | None:
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            return None
        pause = modes[0].pause_seconds
        level = min(
            range(len(INTENSITY_PAUSES)), key=lambda i: abs(INTENSITY_PAUSES[i] - pause)
        )
        return ranged_value_to_percentage(SPEED_RANGE, level + 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            return {}
        return {"run_seconds": modes[0].run_seconds, "pause_seconds": modes[0].pause_seconds}

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        if not self._client.state.power:
            await async_run_command(self._client.set_power(True))
        if percentage:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_run_command(self._client.set_power(False))

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
        level = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        pause = INTENSITY_PAUSES[max(1, min(level, len(INTENSITY_PAUSES))) - 1]
        for mode in modes:
            if mode.pause_seconds != pause:
                await async_run_command(
                    self._client.update_mode(mode.with_changes(pause_seconds=pause))
                )
        if not self._client.state.power:
            await async_run_command(self._client.set_power(True))
