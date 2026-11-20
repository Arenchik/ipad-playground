"""Diagnostics for the TWRA Aroma Diffuser."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import TwraConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TwraConfigEntry
) -> dict[str, Any]:
    client = entry.runtime_data.client
    state = client.state
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_PASSWORD}),
        "options": dict(entry.options),
        "connected": client.connected,
        "identity": asdict(client.identity),
        "power": state.power,
        "light_rgb": state.light_rgb,
        "state_raw": state.raw.hex(),
        "modes": [asdict(mode) | {"days": mode.days} for mode in state.modes],
    }
