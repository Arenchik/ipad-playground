"""The diffuser's lamp, limited to white and blue.

The firmware steps through white -> cyan -> amber -> off; amber is left
out on purpose. Exposing it as an HS light lets HomeKit show its colour
wheel: pale picks give white and any saturated colour gives blue. The
same two choices are effects in the HA UI.
"""
from __future__ import annotations

import colorsys
from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TwraConfigEntry, async_run_command
from .entity import TwraEntity
from .twra import LIGHT_AMBER, LIGHT_CYAN, LIGHT_CYCLE, LIGHT_OFF, LIGHT_WHITE

EFFECTS = {"white": LIGHT_WHITE, "blue": LIGHT_CYAN}
WHITE_SATURATION = 25.0


def _hs(index: int) -> tuple[float, float]:
    red, green, blue = (c / 255 for c in LIGHT_CYCLE[index])
    hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
    return round(hue * 360, 1), round(saturation * 100, 1)


# Amber is still reported if the vendor app set it, never chosen here.
PRESET_HS = {index: _hs(index) for index in (LIGHT_WHITE, LIGHT_CYAN, LIGHT_AMBER)}


def nearest_preset(hs_color: tuple[float, float]) -> int:
    return LIGHT_WHITE if hs_color[1] < WHITE_SATURATION else LIGHT_CYAN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TwraLamp(entry)])


class TwraLamp(TwraEntity, LightEntity):
    _attr_translation_key = "lamp"
    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(EFFECTS)

    def __init__(self, entry: TwraConfigEntry) -> None:
        super().__init__(entry, "lamp")
        self._last_color = LIGHT_WHITE

    @property
    def is_on(self) -> bool | None:
        return self._client.state.light_on

    @property
    def _index(self) -> int | None:
        index = self._client.state.light_index
        if index in EFFECTS.values():
            self._last_color = index
        return index

    @property
    def brightness(self) -> int | None:
        # The lamp has no dimmer; HS mode still needs a brightness value.
        return 255 if self.is_on else None

    @property
    def hs_color(self) -> tuple[float, float] | None:
        index = self._index
        if index is None or index == LIGHT_OFF:
            return None
        return PRESET_HS[index]

    @property
    def effect(self) -> str | None:
        index = self._index
        return next((name for name, value in EFFECTS.items() if value == index), None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_EFFECT in kwargs and kwargs[ATTR_EFFECT] in EFFECTS:
            target = EFFECTS[kwargs[ATTR_EFFECT]]
        elif ATTR_HS_COLOR in kwargs:
            target = nearest_preset(kwargs[ATTR_HS_COLOR])
        elif self.is_on:
            return
        else:
            target = self._last_color
        await async_run_command(self._client.set_light(target))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_run_command(self._client.set_light(LIGHT_OFF))
