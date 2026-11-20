"""One button per scent mode.

HomeKit Bridge exposes a button as a switch that springs back off, so
each mode is a tile in the Home app (and a Siri phrase). Pressing a mode
writes its spray and pause into every enabled working mode and switches
the diffuser on; when it sprays stays up to the working modes' days and
hours, or to the user's own automations.
"""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import TwraConfigEntry, async_run_command
from .const import BOOST_DURATION, BOOST_PAUSE, BOOST_RUN, DOMAIN, SCENT_PRESETS
from .entity import TwraEntity
from .twra import TwraClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TwraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[ButtonEntity] = [
        TwraModeButton(entry, key, run, pause) for key, run, pause in SCENT_PRESETS
    ]
    entities.append(TwraBoostButton(entry))
    async_add_entities(entities)


async def apply_cycle(client: TwraClient, run: int, pause: int) -> None:
    """Set spray/pause on every enabled working mode and power on."""
    modes = [m for m in client.state.modes if m.enabled]
    if not modes:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
    for mode in modes:
        if (mode.run_seconds, mode.pause_seconds) != (run, pause):
            await async_run_command(
                client.update_mode(mode.with_changes(run_seconds=run, pause_seconds=pause))
            )
    if not client.state.power:
        await async_run_command(client.set_power(True))


class TwraModeButton(TwraEntity, ButtonEntity):
    _attr_icon = "mdi:scent"

    def __init__(self, entry: TwraConfigEntry, key: str, run: int, pause: int) -> None:
        super().__init__(entry, f"preset_{key}")
        self._entry = entry
        self._attr_translation_key = f"preset_{key}"
        self._cycle = (run, pause)

    async def async_press(self) -> None:
        # Choosing a mode by hand ends a running guest boost.
        self._entry.runtime_data.cancel_boost()
        await apply_cycle(self._client, *self._cycle)


class TwraBoostButton(TwraEntity, ButtonEntity):
    _attr_translation_key = "preset_guests"
    _attr_icon = "mdi:account-group"

    def __init__(self, entry: TwraConfigEntry) -> None:
        super().__init__(entry, "preset_guests")
        self._entry = entry

    async def async_press(self) -> None:
        data = self._entry.runtime_data
        modes = [m for m in self._client.state.modes if m.enabled]
        if not modes:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
        # A second press extends the boost but keeps the original settings.
        previous = data.boost_restore or (modes[0].run_seconds, modes[0].pause_seconds)
        data.cancel_boost()
        await apply_cycle(self._client, BOOST_RUN, BOOST_PAUSE)
        data.boost_restore = previous

        @callback
        def _restore(_now: datetime) -> None:
            data.boost_unsub = None
            restore, data.boost_restore = data.boost_restore, None
            if restore is None:
                return
            current = [m for m in self._client.state.modes if m.enabled]
            # Leave it alone if the settings were changed during the boost.
            if current and (current[0].run_seconds, current[0].pause_seconds) == (
                BOOST_RUN,
                BOOST_PAUSE,
            ):
                self._entry.async_create_background_task(
                    self.hass, apply_cycle(self._client, *restore), f"{DOMAIN} boost end"
                )

        data.boost_unsub = async_call_later(self.hass, BOOST_DURATION, _restore)
