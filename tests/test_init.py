"""Home Assistant tests: config flow, entities, HomeKit-relevant shape."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, CONF_NAME, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.twra_diffuser.const import CONF_KEEP_CONNECTED, DOMAIN
from custom_components.twra_diffuser.humidifier import (
    intensity_for_pause,
    mode_active_at,
    pause_for_intensity,
)
from custom_components.twra_diffuser.twra import WorkingMode

from .conftest import ADDRESS, NAME

DIFFUSER = "humidifier.aroma_diffuser"
SPRAY = "select.aroma_diffuser_spray_time"
LAMP = "light.aroma_diffuser_lamp"
MODE = "switch.aroma_diffuser_mode_1"


def service_info(ble_device):
    info = MagicMock()
    info.address = ADDRESS
    info.name = NAME
    info.device = ble_device
    return info


@pytest.fixture
def bluetooth(ble_device):
    """Stand in for HA's bluetooth stack."""
    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "homeassistant.components.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.twra_diffuser.config_flow.async_discovered_service_info",
            return_value=[service_info(ble_device)],
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def skip_bluetooth_setup(hass: HomeAssistant):
    hass.config.components.add("bluetooth_adapters")


async def setup_entry(hass: HomeAssistant, keep_connected: bool = True) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Aroma Diffuser",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS},
        options={CONF_KEEP_CONNECTED: keep_connected},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_bluetooth_discovery_flow(hass, bluetooth, links, ble_device) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(ble_device)
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Аромадиффузор"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Аромадиффузор"
    assert result["data"] == {CONF_ADDRESS: ADDRESS}
    assert result["options"] == {CONF_KEEP_CONNECTED: True}
    await hass.async_block_till_done()


async def test_user_flow_password_and_errors(hass, bluetooth, links, diffuser) -> None:
    diffuser.password = "1234"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "X"})
    assert result["step_id"] == "password"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "0000"})
    assert result["errors"] == {"base": "invalid_password"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "1234"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["password"] == "1234"
    await hass.async_block_till_done()


async def test_user_flow_cannot_connect(hass, bluetooth, ble_device) -> None:
    with patch(
        "custom_components.twra_diffuser.twra.establish_connection",
        side_effect=TimeoutError,
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "X"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_entities_and_homekit_shape(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    entities = {e.entity_id: e for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert set(entities) == {
        DIFFUSER,
        LAMP,
        SPRAY,
        MODE,
        "number.aroma_diffuser_mode_1_spray_time",
        "number.aroma_diffuser_mode_1_pause",
        "time.aroma_diffuser_mode_1_start",
        "time.aroma_diffuser_mode_1_end",
    }
    # HomeKit Bridge only picks up entities without a category.
    exposed = {eid for eid, e in entities.items() if e.entity_category is None}
    assert exposed == {DIFFUSER, LAMP, SPRAY}
    assert all(
        entities[eid].entity_category is EntityCategory.CONFIG for eid in set(entities) - exposed
    )

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.name == "Aroma Diffuser"
    assert device.serial_number == "TWRA01BB2691401B2"

    state = hass.states.get(DIFFUSER)
    assert state.state == "on"
    assert state.attributes["device_class"] == "humidifier"
    assert state.attributes["humidity"] == state.attributes["current_humidity"] == 18
    assert state.attributes["pause_seconds"] == 500
    assert hass.states.get(SPRAY).state == "16 s"
    assert hass.states.get(LAMP).state == "on"
    assert hass.states.get(LAMP).attributes["effect"] == "white"
    assert hass.states.get(MODE).attributes["days"] == ["mon", "wed", "fri", "sun"]
    assert hass.states.get("time.aroma_diffuser_mode_1_end").state == "23:59:00"


async def test_old_fan_entity_removed(hass, bluetooth, links, diffuser) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("fan", DOMAIN, f"{ADDRESS}_diffuser", suggested_object_id="old")
    await setup_entry(hass)
    assert registry.async_get("fan.old") is None


async def test_diffuser_controls(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)

    await hass.services.async_call("humidifier", "turn_off", {"entity_id": DIFFUSER}, blocking=True)
    assert diffuser.power is False
    assert hass.states.get(DIFFUSER).state == "off"
    assert hass.states.get(DIFFUSER).attributes["action"] == "off"
    await hass.services.async_call("humidifier", "turn_on", {"entity_id": DIFFUSER}, blocking=True)
    assert diffuser.power is True

    for percent, pause in ((100, 30), (1, 900), (50, 170)):
        await hass.services.async_call(
            "humidifier", "set_humidity", {"entity_id": DIFFUSER, "humidity": percent}, blocking=True
        )
        assert diffuser.modes[0].pause_seconds == pause
        assert hass.states.get(DIFFUSER).attributes["humidity"] == percent
    assert diffuser.modes[0].run_seconds == 16


async def test_spray_time_select(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)
    assert hass.states.get(SPRAY).attributes["options"] == [
        "5 s", "10 s", "16 s", "20 s", "30 s", "45 s", "60 s"
    ]
    await hass.services.async_call(
        "select", "select_option", {"entity_id": SPRAY, "option": "30 s"}, blocking=True
    )
    assert diffuser.modes[0].run_seconds == 30
    assert diffuser.modes[0].pause_seconds == 500
    assert hass.states.get(SPRAY).state == "30 s"


async def test_needs_an_enabled_mode(hass, bluetooth, links, diffuser) -> None:
    diffuser.modes = [diffuser.modes[0].with_changes(enabled=False)]
    await setup_entry(hass)
    assert hass.states.get(SPRAY).state == "unknown"
    with pytest.raises(HomeAssistantError, match="none is enabled"):
        await hass.services.async_call(
            "humidifier", "set_humidity", {"entity_id": DIFFUSER, "humidity": 50}, blocking=True
        )


def test_intensity_scale() -> None:
    assert pause_for_intensity(1) == 900
    assert pause_for_intensity(100) == 30
    pauses = [pause_for_intensity(p) for p in range(1, 101)]
    assert pauses == sorted(pauses, reverse=True)
    for percent in range(1, 101):
        assert abs(intensity_for_pause(pause_for_intensity(percent)) - percent) <= 2
    assert intensity_for_pause(500) == 18


def test_schedule_window() -> None:
    mode = WorkingMode.from_bytes(bytes.fromhex("0101550000009f051000f40178030000"))
    friday_noon = datetime(2026, 11, 20, 12, 0)
    assert mode_active_at(mode, friday_noon)
    assert not mode_active_at(mode, datetime(2026, 11, 19, 12, 0))  # Thursday
    night = mode.with_changes(start_minute=22 * 60, end_minute=6 * 60)
    assert mode_active_at(night, datetime(2026, 11, 20, 23, 0))  # Fri 23:00
    assert mode_active_at(night, datetime(2026, 11, 21, 5, 0))  # Sat 05:00, Fri's window
    assert not mode_active_at(night, datetime(2026, 11, 20, 5, 0))  # Fri 05:00, Thu's window


async def test_lamp_snaps_homekit_colours(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass)

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "hs_color": [210, 90]}, blocking=True
    )
    assert diffuser.light == 1  # cyan
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "hs_color": [10, 100]}, blocking=True
    )
    assert diffuser.light == 2  # amber
    await hass.services.async_call("light", "turn_off", {"entity_id": LAMP}, blocking=True)
    assert diffuser.light == 3
    assert hass.states.get(LAMP).state == "off"
    # Turning on again restores the last colour.
    await hass.services.async_call("light", "turn_on", {"entity_id": LAMP}, blocking=True)
    assert diffuser.light == 2
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "effect": "white"}, blocking=True
    )
    assert diffuser.light == 0


async def test_mode_entities_and_service(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass)

    await hass.services.async_call("switch", "turn_off", {"entity_id": MODE}, blocking=True)
    assert diffuser.modes[0].enabled is False
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.aroma_diffuser_mode_1_spray_time", "value": 20},
        blocking=True,
    )
    assert diffuser.modes[0].run_seconds == 20
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": "time.aroma_diffuser_mode_1_start", "time": "08:30"},
        blocking=True,
    )
    assert diffuser.modes[0].start_minute == 8 * 60 + 30

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    await hass.services.async_call(
        DOMAIN,
        "set_working_mode",
        {"device_id": device.id, "mode": 1, "enabled": True, "days": ["sat", "sun"], "end": "22:00"},
        blocking=True,
    )
    mode = diffuser.modes[0]
    assert mode.enabled and mode.days == ["sat", "sun"] and mode.end_minute == 22 * 60
    assert hass.states.get(MODE).state == "on"


async def test_unavailable_then_reconnects(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass)
    links[-1].drop()
    await hass.async_block_till_done()
    # The background reconnect opens a fresh link straight away.
    assert len(links) == 2
    assert hass.states.get(DIFFUSER).state == "on"


async def test_on_demand_mode_and_unload(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass, keep_connected=False)
    await hass.services.async_call("humidifier", "turn_off", {"entity_id": DIFFUSER}, blocking=True)
    assert diffuser.power is False
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not links[-1].is_connected
