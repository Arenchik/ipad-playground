"""TWRA Aroma Diffuser (Tersway TW-RA01) over Bluetooth."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
# Imported as dt_time: once the time platform loads, the package attribute
# `time` is that submodule.
from datetime import datetime, time as dt_time

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DAYS,
    ATTR_DEVICE_ID,
    ATTR_ENABLED,
    ATTR_END,
    ATTR_MODE,
    ATTR_PAUSE_SECONDS,
    ATTR_RUN_SECONDS,
    ATTR_START,
    CONF_KEEP_CONNECTED,
    DEFAULT_KEEP_CONNECTED,
    DOMAIN,
    IDLE_TIMEOUT,
    REFRESH_INTERVAL,
    SERVICE_SET_WORKING_MODE,
    TIME_SYNC_INTERVAL,
)
from .twra import WEEKDAYS, InvalidPassword, PasswordRequired, TwraClient, TwraError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.HUMIDIFIER,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TIME,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SET_WORKING_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_MODE): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
        vol.Optional(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_DAYS): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)]),
        vol.Optional(ATTR_START): cv.time,
        vol.Optional(ATTR_END): cv.time,
        vol.Optional(ATTR_RUN_SECONDS): vol.All(vol.Coerce(int), vol.Range(min=1, max=600)),
        vol.Optional(ATTR_PAUSE_SECONDS): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
    }
)


@dataclass
class TwraData:
    client: TwraClient
    mode_serials: frozenset[int]
    reconnect_task: asyncio.Task | None = field(default=None)
    closing: bool = False


type TwraConfigEntry = ConfigEntry[TwraData]


def minute_to_time(minute: int) -> dt_time:
    return dt_time(minute // 60 % 24, minute % 60)


def time_to_minute(value: dt_time) -> int:
    return value.hour * 60 + value.minute


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_set_working_mode(call: ServiceCall) -> None:
        client = _client_for_device(hass, call.data[ATTR_DEVICE_ID])
        number = call.data[ATTR_MODE]
        mode = next((m for m in client.state.modes if m.serial == number), None)
        if mode is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_mode",
                translation_placeholders={"mode": str(number)},
            )
        if ATTR_DAYS in call.data:
            mode = mode.with_days(call.data[ATTR_DAYS])
        changes = {}
        if ATTR_ENABLED in call.data:
            changes["enabled"] = call.data[ATTR_ENABLED]
        if ATTR_START in call.data:
            changes["start_minute"] = time_to_minute(call.data[ATTR_START])
        if ATTR_END in call.data:
            changes["end_minute"] = time_to_minute(call.data[ATTR_END])
        if ATTR_RUN_SECONDS in call.data:
            changes["run_seconds"] = call.data[ATTR_RUN_SECONDS]
        if ATTR_PAUSE_SECONDS in call.data:
            changes["pause_seconds"] = call.data[ATTR_PAUSE_SECONDS]
        await async_run_command(client.update_mode(mode.with_changes(**changes)))

    hass.services.async_register(
        DOMAIN, SERVICE_SET_WORKING_MODE, handle_set_working_mode, SET_WORKING_MODE_SCHEMA
    )
    return True


def _client_for_device(hass: HomeAssistant, device_id: str) -> TwraClient:
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN and hasattr(entry, "runtime_data"):
                return entry.runtime_data.client
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_device")


async def async_run_command(command) -> None:
    """Await a client command, turning BLE failures into HA errors."""
    try:
        await command
    except (BleakError, TimeoutError, TwraError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err) or type(err).__name__},
        ) from err


async def async_setup_entry(hass: HomeAssistant, entry: TwraConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    keep_connected = entry.options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED)

    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="not_found",
            translation_placeholders={"address": address},
        )

    client = TwraClient(
        ble_device,
        password=entry.data.get(CONF_PASSWORD),
        keep_connected=keep_connected,
        idle_timeout=IDLE_TIMEOUT,
        now=dt_util.now,
    )
    try:
        await client.connect()
    except (PasswordRequired, InvalidPassword) as err:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="invalid_password") from err
    except (BleakError, TimeoutError, TwraError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err) or type(err).__name__},
        ) from err

    data = TwraData(client, frozenset(m.serial for m in client.state.modes))
    entry.runtime_data = data

    # 1.0 exposed the diffuser as a fan; it is a humidifier since 1.1.
    registry = er.async_get(hass)
    if old_fan := registry.async_get_entity_id("fan", DOMAIN, f"{client.address}_diffuser"):
        registry.async_remove(old_fan)

    async def _reconnect() -> None:
        delay = 5
        while not client.connected:
            try:
                await client.connect()
            except (BleakError, TimeoutError, TwraError) as err:
                _LOGGER.debug("%s: reconnect failed: %s", client.name, err)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    @callback
    def _ensure_reconnect() -> None:
        if not keep_connected or client.connected or data.closing:
            return
        if data.reconnect_task is None or data.reconnect_task.done():
            data.reconnect_task = entry.async_create_background_task(
                hass, _reconnect(), f"{DOMAIN} reconnect {address}"
            )

    @callback
    def _on_client_update() -> None:
        serials = frozenset(m.serial for m in client.state.modes)
        if client.state.modes and serials != data.mode_serials:
            # Modes were added or removed in the vendor app: rebuild entities.
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        _ensure_reconnect()

    @callback
    def _on_advertisement(
        service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        client.set_ble_device(service_info.device)
        _ensure_reconnect()

    async def _refresh(_now: datetime) -> None:
        if keep_connected and not client.connected:
            return
        try:
            await client.refresh()
        except (BleakError, TimeoutError, TwraError) as err:
            _LOGGER.debug("%s: refresh failed: %s", client.name, err)

    async def _sync_time(_now: datetime) -> None:
        if not client.connected:
            return
        try:
            await client.sync_time()
        except (BleakError, TimeoutError, TwraError) as err:
            _LOGGER.debug("%s: time sync failed: %s", client.name, err)

    entry.async_on_unload(client.register_callback(_on_client_update))
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=address.upper(), connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    entry.async_on_unload(async_track_time_interval(hass, _refresh, REFRESH_INTERVAL))
    entry.async_on_unload(async_track_time_interval(hass, _sync_time, TIME_SYNC_INTERVAL))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: TwraConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TwraConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.closing = True
        await entry.runtime_data.client.disconnect()
    return unloaded
