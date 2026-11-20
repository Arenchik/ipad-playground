"""Scent modes as remote activities, for a pop-up list in HomeKit.

The only HomeKit tile with a list of named choices is the television's
input picker. HomeKit Bridge builds one from a remote with activities, so
the diffuser becomes a TV-style accessory: the tile toggles power and its
menu picks a scent mode. Activity names are what HomeKit shows, hence the
localised labels with the spray and pause spelled out.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import ATTR_ACTIVITY, RemoteEntity, RemoteEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TwraConfigEntry, async_run_command
from .const import DOMAIN, PRESET_CUSTOM, SCENT_PRESETS
from .entity import TwraEntity

LABELS = {
    "ru": {
        "faint": "Едва заметно",
        "light": "Легко",
        "standard": "Стандарт",
        "rich": "Насыщенно",
        "strong": "Сильно",
        "intense": "Интенсивно",
        "max": "Максимум",
        PRESET_CUSTOM: "Свои настройки",
    },
    "en": {
        "faint": "Faint",
        "light": "Light",
        "standard": "Standard",
        "rich": "Rich",
        "strong": "Strong",
        "intense": "Intense",
        "max": "Maximum",
        PRESET_CUSTOM: "Custom settings",
    },
}
UNITS = {"ru": ("с", "мин"), "en": ("s", "min")}
TEMPLATES = {"ru": "{name} — {run}, пауза {pause}", "en": "{name} — {run}, {pause} pause"}


def _duration(seconds: int, lang: str) -> str:
    sec, minute = UNITS[lang]
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds // 60} {minute}"
    return f"{seconds} {sec}"


def preset_labels(lang: str) -> dict[str, str]:
    """Preset key -> label such as "Стандарт — 16 с, пауза 8 мин"."""
    lang = "ru" if lang.startswith("ru") else "en"
    labels = {
        key: TEMPLATES[lang].format(
            name=LABELS[lang][key], run=_duration(run, lang), pause=_duration(pause, lang)
        )
        for key, run, pause in SCENT_PRESETS
    }
    labels[PRESET_CUSTOM] = LABELS[lang][PRESET_CUSTOM]
    return labels


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TwraScentModes(entry, preset_labels(hass.config.language or "en"))])


class TwraScentModes(TwraEntity, RemoteEntity):
    _attr_name = None
    _attr_icon = "mdi:scent"
    _attr_supported_features = RemoteEntityFeature.ACTIVITY

    def __init__(self, entry: TwraConfigEntry, labels: dict[str, str]) -> None:
        super().__init__(entry, "scent_modes")
        self._labels = labels
        self._presets = {key: (run, pause) for key, run, pause in SCENT_PRESETS}
        self._attr_activity_list = list(labels.values())

    @property
    def is_on(self) -> bool | None:
        return self._client.state.power

    @property
    def current_activity(self) -> str | None:
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            return None
        current = (modes[0].run_seconds, modes[0].pause_seconds)
        key = next((k for k, v in self._presets.items() if v == current), PRESET_CUSTOM)
        return self._labels[key]

    async def async_turn_on(self, activity: str | None = None, **kwargs: Any) -> None:
        activity = activity or kwargs.get(ATTR_ACTIVITY)
        if activity is not None:
            await self._apply(activity)
        if not self._client.state.power:
            await async_run_command(self._client.set_power(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_run_command(self._client.set_power(False))

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_commands")

    async def _apply(self, activity: str) -> None:
        key = next((k for k, label in self._labels.items() if label == activity), None)
        if key is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unknown_scent_mode",
                translation_placeholders={"mode": activity},
            )
        if key == PRESET_CUSTOM:
            # Nothing to apply: the values set elsewhere stay as they are.
            return
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
        run, pause = self._presets[key]
        for mode in modes:
            if (mode.run_seconds, mode.pause_seconds) != (run, pause):
                await async_run_command(
                    self._client.update_mode(mode.with_changes(run_seconds=run, pause_seconds=pause))
                )
