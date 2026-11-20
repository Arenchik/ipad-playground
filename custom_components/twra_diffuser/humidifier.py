"""Scent intensity as a humidifier: power plus a percentage slider.

HomeKit has no diffuser service; its humidifier is the closest match (a
mist icon, an on/off toggle and one percentage slider), and Siri handles
"set the intensity to 40%". The slider sets the pause between sprays of
the enabled working modes on a log scale, so low values are fine-grained.
The named scent modes are buttons.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from . import TwraConfigEntry, async_run_command
from .const import DOMAIN, MAX_PAUSE, MIN_PAUSE
from .entity import TwraEntity
from .twra import WorkingMode


def pause_for_intensity(percent: float) -> int:
    """1 % -> MAX_PAUSE seconds, 100 % -> MIN_PAUSE, logarithmic in between."""
    percent = min(100.0, max(1.0, percent))
    raw = MAX_PAUSE * (MIN_PAUSE / MAX_PAUSE) ** ((percent - 1) / 99)
    step = 10 if raw >= 120 else 5
    return int(min(MAX_PAUSE, max(MIN_PAUSE, round(raw / step) * step)))


def intensity_for_pause(pause: int) -> int:
    pause = min(MAX_PAUSE, max(MIN_PAUSE, pause))
    percent = 1 + 99 * math.log(pause / MAX_PAUSE) / math.log(MIN_PAUSE / MAX_PAUSE)
    return int(min(100, max(1, round(percent))))


def mode_active_at(mode: WorkingMode, now: datetime) -> bool:
    """Whether the mode's schedule window covers `now` (local time)."""
    if not mode.enabled:
        return False
    minute = now.hour * 60 + now.minute
    weekday = now.weekday()
    if mode.start_minute <= mode.end_minute:
        return bool(mode.weekdays & (1 << weekday)) and mode.start_minute <= minute <= mode.end_minute
    # Window wraps past midnight: the early part belongs to yesterday's day.
    if minute >= mode.start_minute:
        return bool(mode.weekdays & (1 << weekday))
    return minute <= mode.end_minute and bool(mode.weekdays & (1 << ((weekday - 1) % 7)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TwraDiffuser(entry)])


class TwraDiffuser(TwraEntity, HumidifierEntity):
    # Named so the bare percentage in HomeKit reads as "Intensity 21 %".
    _attr_translation_key = "intensity"
    _attr_device_class = HumidifierDeviceClass.HUMIDIFIER
    _attr_min_humidity = 1
    _attr_max_humidity = 100

    def __init__(self, entry: TwraConfigEntry) -> None:
        super().__init__(entry, "diffuser")
        # The slider value last set, kept so the round trip through whole
        # seconds of pause doesn't nudge HomeKit's slider by a percent.
        self._requested: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # `action` depends on the clock, not only on device pushes.
        self.async_on_remove(async_track_time_change(self.hass, self._tick, second=0))

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def _enabled_modes(self) -> list[WorkingMode]:
        return [m for m in self._client.state.modes if m.enabled]

    @property
    def is_on(self) -> bool | None:
        return self._client.state.power

    @property
    def target_humidity(self) -> int | None:
        modes = self._enabled_modes
        if not modes:
            return None
        pause = modes[0].pause_seconds
        if self._requested is not None and pause_for_intensity(self._requested) == pause:
            return self._requested
        return intensity_for_pause(pause)

    @property
    def current_humidity(self) -> int | None:
        # HomeKit shows this as the tile's big number; mirror the intensity
        # rather than leave it at 0 %.
        return self.target_humidity

    @property
    def action(self) -> HumidifierAction | None:
        if self.is_on is None:
            return None
        if not self.is_on:
            return HumidifierAction.OFF
        now = dt_util.now()
        if any(mode_active_at(m, now) for m in self._enabled_modes):
            return HumidifierAction.HUMIDIFYING
        return HumidifierAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        modes = self._enabled_modes
        if not modes:
            return {}
        return {"run_seconds": modes[0].run_seconds, "pause_seconds": modes[0].pause_seconds}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_run_command(self._client.set_power(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_run_command(self._client.set_power(False))

    async def async_set_humidity(self, humidity: int) -> None:
        modes = self._enabled_modes
        if not modes:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
        pause = pause_for_intensity(humidity)
        self._requested = int(humidity)
        for mode in modes:
            if mode.pause_seconds != pause:
                await async_run_command(
                    self._client.update_mode(mode.with_changes(pause_seconds=pause))
                )
        self.async_write_ha_state()
