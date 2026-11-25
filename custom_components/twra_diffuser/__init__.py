"""TWRA Aroma Diffuser (Tersway TW-RA01) over Bluetooth."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import voluptuous as vol
from bleak.backends.device import BLEDevice
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
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DEVICE_ID,
    ATTR_PAUSE_SECONDS,
    ATTR_RUN_SECONDS,
    CONF_KEEP_CONNECTED,
    DEFAULT_KEEP_CONNECTED,
    DOMAIN,
    IDLE_TIMEOUT,
    OFFLINE_AFTER,
    RECONNECT_MAX,
    RECONNECT_MIN,
    REFRESH_INTERVAL,
    SERVICE_SET_SPRAY_CYCLE,
    STORE_VERSION,
    TIME_SYNC_INTERVAL,
)
from .twra import (
    WEEKDAYS,
    InvalidPassword,
    PasswordRequired,
    TwraClient,
    DeviceIdentity,
    DiffuserState,
    TwraError,
    WorkingMode,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BUTTON,
    Platform.HUMIDIFIER,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SET_SPRAY_CYCLE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_DEVICE_ID): cv.string,
            vol.Optional(ATTR_RUN_SECONDS): vol.All(vol.Coerce(int), vol.Range(min=1, max=600)),
            vol.Optional(ATTR_PAUSE_SECONDS): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
        }
    ),
    cv.has_at_least_one_key(ATTR_RUN_SECONDS, ATTR_PAUSE_SECONDS),
)


@dataclass
class TwraData:
    client: TwraClient
    mode_serials: frozenset[int]
    reconnect_task: asyncio.Task | None = field(default=None)
    # Set when the diffuser is heard advertising, to retry without waiting.
    reconnect_wake: asyncio.Event = field(default_factory=asyncio.Event)
    closing: bool = False
    # False once the diffuser has been neither heard nor connected for
    # OFFLINE_AFTER. Entities stay available until then.
    present: bool = True
    offline_unsub: Callable[[], None] | None = None
    store: Store | None = None
    schedule_opened: bool = False
    # Guest boost: settings to restore and the timer that restores them.
    boost_restore: tuple[int, int] | None = None
    boost_unsub: Callable[[], None] | None = None

    def cancel_boost(self) -> None:
        if self.boost_unsub is not None:
            self.boost_unsub()
            self.boost_unsub = None
        self.boost_restore = None

    def cancel_offline(self) -> None:
        if self.offline_unsub is not None:
            self.offline_unsub()
            self.offline_unsub = None


type TwraConfigEntry = ConfigEntry[TwraData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_set_spray_cycle(call: ServiceCall) -> None:
        client = _client_for_device(hass, call.data[ATTR_DEVICE_ID])
        changes = {
            key: call.data[key] for key in (ATTR_RUN_SECONDS, ATTR_PAUSE_SECONDS) if key in call.data
        }
        await async_apply_cycle(client, **changes)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SPRAY_CYCLE, handle_set_spray_cycle, SET_SPRAY_CYCLE_SCHEMA
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


FULL_DAY_END = 23 * 60 + 59


def full_time(mode: WorkingMode) -> WorkingMode:
    """The mode running every day, all day; days are left to automations."""
    return mode.with_days(list(WEEKDAYS)).with_changes(
        enabled=True, start_minute=0, end_minute=FULL_DAY_END
    )


async def async_apply_cycle(client: TwraClient, **changes: int) -> None:
    """Write spray/pause `changes` into the working modes, kept full-time.

    Uses the enabled modes, or the first one if none is enabled, so a
    mode switched off in the vendor app comes back on.
    """
    modes = [m for m in client.state.modes if m.enabled] or list(client.state.modes[:1])
    if not modes:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_enabled_mode")
    for mode in modes:
        target = full_time(mode).with_changes(**changes)
        if target != mode:
            await async_run_command(client.update_mode(target))


def _snapshot(client: TwraClient) -> dict:
    state = client.state
    return {
        "power": state.power,
        "light_rgb": list(state.light_rgb) if state.light_rgb else None,
        "modes": [mode.to_bytes().hex() for mode in state.modes],
        "serial": client.identity.serial,
        "hardware_id": client.identity.hardware_id,
    }


def _restore(client: TwraClient, snapshot: dict) -> None:
    try:
        state = DiffuserState(
            power=snapshot.get("power"),
            light_rgb=tuple(snapshot["light_rgb"]) if snapshot.get("light_rgb") else None,
            modes=tuple(WorkingMode.from_bytes(bytes.fromhex(m)) for m in snapshot.get("modes", [])),
        )
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.debug("%s: ignoring unreadable saved state: %s", client.name, err)
        return
    client.restore(state, DeviceIdentity(snapshot.get("serial"), snapshot.get("hardware_id")))


async def _async_open_schedule(data: TwraData) -> None:
    """Days and hours belong to the user's automations: lift any schedule
    set in the vendor app so the diffuser can spray whenever it is on."""
    if data.schedule_opened or not data.client.connected:
        return
    try:
        await async_apply_cycle(data.client)
    except HomeAssistantError as err:
        _LOGGER.warning("%s: could not open the schedule to all day: %s", data.client.name, err)
    else:
        data.schedule_opened = True


async def async_setup_entry(hass: HomeAssistant, entry: TwraConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    keep_connected = entry.options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED)

    store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
    snapshot = await store.async_load()

    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), connectable=True)
    heard = ble_device is not None
    client = TwraClient(
        # Not heard yet: a placeholder until the first advertisement.
        ble_device or BLEDevice(address.upper(), entry.title, None),
        password=entry.data.get(CONF_PASSWORD),
        keep_connected=keep_connected,
        idle_timeout=IDLE_TIMEOUT,
        now=dt_util.now,
    )
    if snapshot:
        _restore(client, snapshot)

    # The diffuser may be out of reach right now. With a saved state the
    # integration starts anyway and connects in the background; failing
    # setup here would leave HomeKit with "No Response" until HA retries.
    error: Exception | None = None
    if heard:
        try:
            await client.connect()
        except (PasswordRequired, InvalidPassword) as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="invalid_password"
            ) from err
        except (BleakError, TimeoutError, TwraError) as err:
            error = err
    if not client.connected:
        if not client.state.modes:
            # First start ever: nothing to show until it has connected once.
            if error is None:
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="not_found",
                    translation_placeholders={"address": address},
                )
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(error) or type(error).__name__},
            ) from error
        _LOGGER.info(
            "%s: not reachable at start (%s); using the last known state",
            client.name,
            error or "not heard yet",
        )

    data = TwraData(client, frozenset(m.serial for m in client.state.modes), store=store)
    entry.runtime_data = data
    await _async_open_schedule(data)

    # Entities of platforms later versions dropped (fan, remote, the
    # schedule switches and times).
    registry = er.async_get(hass)
    platforms = {str(platform) for platform in PLATFORMS}
    for old in er.async_entries_for_config_entry(registry, entry.entry_id):
        if old.domain not in platforms:
            registry.async_remove(old.entity_id)

    async def _reconnect() -> None:
        delay = RECONNECT_MIN
        while not client.connected and not data.closing:
            attempt = hass.loop.time()
            data.reconnect_wake.clear()
            try:
                await client.connect()
            except (BleakError, TimeoutError, TwraError) as err:
                _LOGGER.debug("%s: reconnect failed: %s", client.name, err)
            else:
                _mark_present()
                await _async_open_schedule(data)
                return
            # Wait for the back-off, or less if the diffuser is heard again
            # (carried back into range, plugged in, rebooted)...
            try:
                async with asyncio.timeout(delay):
                    await data.reconnect_wake.wait()
            except TimeoutError:
                delay = min(delay * 2, RECONNECT_MAX)
            else:
                # ...but never retry more often than every RECONNECT_MIN.
                delay = RECONNECT_MIN
                await asyncio.sleep(max(0.0, attempt + RECONNECT_MIN - hass.loop.time()))

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
        if client.connected:
            _mark_present()
        elif not bluetooth.async_address_present(hass, address.upper(), connectable=True):
            # Link lost while HA's Bluetooth already counts it as gone.
            _start_offline_timer()
        _ensure_reconnect()

    @callback
    def _mark_present() -> None:
        data.cancel_offline()
        if not data.present:
            data.present = True
            client.notify()

    @callback
    def _go_offline(_now: datetime) -> None:
        data.offline_unsub = None
        if client.connected or data.closing:
            return
        _LOGGER.info("%s: not heard for %s, marking unavailable", client.name, OFFLINE_AFTER)
        data.present = False
        client.notify()

    @callback
    def _start_offline_timer() -> None:
        if data.offline_unsub is None and data.present:
            data.offline_unsub = async_call_later(hass, OFFLINE_AFTER, _go_offline)

    @callback
    def _on_advertisement(
        service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        client.set_ble_device(service_info.device)
        _mark_present()
        data.reconnect_wake.set()
        _ensure_reconnect()

    @callback
    def _on_unavailable(_service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        # HA's Bluetooth missed its adverts for a few minutes. At the edge of
        # range that happens all the time, so wait before giving up on it.
        if not client.connected:
            _start_offline_timer()

    @callback
    def _save_state() -> None:
        if client.state.modes:
            store.async_delay_save(lambda: _snapshot(client), 10)

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
    entry.async_on_unload(client.register_callback(_save_state))
    entry.async_on_unload(
        bluetooth.async_track_unavailable(hass, _on_unavailable, address.upper(), connectable=True)
    )
    entry.async_on_unload(data.cancel_offline)
    _save_state()
    if not heard and not client.connected:
        # Started from the saved state: shown as before until proven gone.
        _start_offline_timer()
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
    # Started with the last known state: keep trying in the background.
    _ensure_reconnect()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: TwraConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TwraConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = entry.runtime_data
        data.closing = True
        data.cancel_boost()
        data.cancel_offline()
        await data.client.disconnect()
        if data.store is not None and data.client.state.modes:
            await data.store.async_save(_snapshot(data.client))
    return unloaded
