"""Home Assistant tests: config flow, entities, HomeKit-relevant shape."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, CONF_NAME, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.twra_diffuser.const import CONF_KEEP_CONNECTED, DOMAIN
from custom_components.twra_diffuser.humidifier import (
    intensity_for_pause,
    mode_active_at,
    pause_for_intensity,
)
from custom_components.twra_diffuser.twra import WorkingMode

from .conftest import ADDRESS, NAME

DIFFUSER = "humidifier.aroma_diffuser_intensity"
BUTTONS = {
    key: f"button.aroma_diffuser_{key}"
    for key in ("minimum", "eco", "balanced", "rich", "guests_for_20_min")
}
SPRAY = "select.aroma_diffuser_spray_time"
LAMP = "light.aroma_diffuser_lamp"


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
        *BUTTONS.values(),
        "number.aroma_diffuser_mode_1_spray_time",
        "number.aroma_diffuser_mode_1_pause",
    }
    # HomeKit Bridge only picks up entities without a category.
    exposed = {eid for eid, e in entities.items() if e.entity_category is None}
    assert exposed == {DIFFUSER, LAMP, *BUTTONS.values()}
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


async def test_spray_time_leaves_homekit_after_upgrade(hass, bluetooth, links, diffuser) -> None:
    # 1.1 registered it without a category, so HomeKit Bridge exposed it.
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "select", DOMAIN, f"{ADDRESS}_spray_time", suggested_object_id="aroma_diffuser_spray_time"
    )
    await setup_entry(hass)
    assert registry.async_get(SPRAY).entity_category is EntityCategory.CONFIG


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


ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def assert_full_time(mode) -> None:
    assert mode.enabled
    assert mode.days == ALL_DAYS
    assert (mode.start_minute, mode.end_minute) == (0, 23 * 60 + 59)


async def test_setup_opens_schedule_to_every_day(hass, bluetooth, links, diffuser) -> None:
    # The unit came with Mon/Wed/Fri/Sun only, from the vendor app.
    assert diffuser.modes[0].days == ["mon", "wed", "fri", "sun"]
    await setup_entry(hass)
    assert_full_time(diffuser.modes[0])
    # Only the schedule changes, not how much it sprays.
    assert cycle(diffuser) == (16, 500)


async def test_setup_reenables_a_disabled_mode(hass, bluetooth, links, diffuser) -> None:
    diffuser.modes = [diffuser.modes[0].with_changes(enabled=False)]
    await setup_entry(hass)
    assert_full_time(diffuser.modes[0])
    assert hass.states.get(SPRAY).state == "16 s"


async def test_buttons_reopen_a_schedule_changed_in_app(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass)
    # Narrowed again in the vendor app; HA picks it up on its next read.
    diffuser.modes = [diffuser.modes[0].with_days(["sat"]).with_changes(start_minute=600)]
    await entry.runtime_data.client.refresh()
    await press(hass, BUTTONS["balanced"])
    assert_full_time(diffuser.modes[0])
    assert cycle(diffuser) == (12, 480)


async def test_no_working_mode_at_all(hass, bluetooth, links, diffuser) -> None:
    diffuser.modes = []
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="no working mode"):
        await press(hass, BUTTONS["eco"])


async def test_entities_of_dropped_platforms_removed(hass, bluetooth, links, diffuser) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Aroma Diffuser", unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # 1.0 fan, 1.2 TV remote, schedule switch and times from before 1.4.
    for domain, key in (
        ("fan", "diffuser"),
        ("remote", "scent_modes"),
        ("switch", "mode1_enabled"),
        ("time", "mode1_start"),
        ("time", "mode1_end"),
    ):
        registry.async_get_or_create(domain, DOMAIN, f"{ADDRESS}_{key}", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    domains = {e.domain for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert domains == {"button", "humidifier", "light", "number", "select"}


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

    assert hass.states.get(LAMP).attributes["effect_list"] == ["white", "blue"]
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "hs_color": [210, 90]}, blocking=True
    )
    assert diffuser.light == 1  # blue
    # Any saturated colour, even red, means blue: amber is never chosen.
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "hs_color": [10, 100]}, blocking=True
    )
    assert diffuser.light == 1
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "hs_color": [30, 10]}, blocking=True
    )
    assert diffuser.light == 0  # pale -> white
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LAMP, "effect": "blue"}, blocking=True
    )
    await hass.services.async_call("light", "turn_off", {"entity_id": LAMP}, blocking=True)
    assert diffuser.light == 3
    assert hass.states.get(LAMP).state == "off"
    # Turning on again restores the last colour.
    await hass.services.async_call("light", "turn_on", {"entity_id": LAMP}, blocking=True)
    assert diffuser.light == 1


async def test_lamp_ignores_amber_set_in_app(hass, bluetooth, links, diffuser) -> None:
    diffuser.light = 2
    await setup_entry(hass)
    assert hass.states.get(LAMP).attributes["effect"] is None
    await hass.services.async_call("light", "turn_off", {"entity_id": LAMP}, blocking=True)
    await hass.services.async_call("light", "turn_on", {"entity_id": LAMP}, blocking=True)
    assert diffuser.light == 0


def cycle(diffuser) -> tuple[int, int]:
    return diffuser.modes[0].run_seconds, diffuser.modes[0].pause_seconds


async def press(hass, entity_id) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_mode_buttons(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)
    expected = {"minimum": (8, 900), "eco": (10, 600), "balanced": (12, 480), "rich": (16, 300)}
    for key, values in expected.items():
        await press(hass, BUTTONS[key])
        assert cycle(diffuser) == values
    # Weakest first, and every mode uses less oil per hour than the next.
    duty = [run / (run + pause) for run, pause in expected.values()]
    assert duty == sorted(duty)

    # A mode press also switches the diffuser on.
    await hass.services.async_call("humidifier", "turn_off", {"entity_id": DIFFUSER}, blocking=True)
    await press(hass, BUTTONS["eco"])
    assert diffuser.power is True


async def later(hass, minutes: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_guests_boost_restores(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)
    await press(hass, BUTTONS["eco"])
    await press(hass, BUTTONS["guests_for_20_min"])
    assert cycle(diffuser) == (30, 120)
    await later(hass, 19)
    assert cycle(diffuser) == (30, 120)
    await later(hass, 21)
    assert cycle(diffuser) == (10, 600)


async def test_second_boost_press_keeps_original(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)
    await press(hass, BUTTONS["eco"])
    await press(hass, BUTTONS["guests_for_20_min"])
    await press(hass, BUTTONS["guests_for_20_min"])
    await later(hass, 21)
    assert cycle(diffuser) == (10, 600)


async def test_mode_press_ends_boost(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)
    await press(hass, BUTTONS["eco"])
    await press(hass, BUTTONS["guests_for_20_min"])
    await press(hass, BUTTONS["rich"])
    await later(hass, 25)
    assert cycle(diffuser) == (16, 300)


async def test_boost_left_alone_if_changed(hass, bluetooth, links, diffuser) -> None:
    await setup_entry(hass)
    await press(hass, BUTTONS["eco"])
    await press(hass, BUTTONS["guests_for_20_min"])
    await hass.services.async_call(
        "humidifier", "set_humidity", {"entity_id": DIFFUSER, "humidity": 100}, blocking=True
    )
    await later(hass, 25)
    assert cycle(diffuser) == (30, 30)


async def test_number_entities_and_service(hass, bluetooth, links, diffuser) -> None:
    entry = await setup_entry(hass)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.aroma_diffuser_mode_1_spray_time", "value": 20},
        blocking=True,
    )
    assert diffuser.modes[0].run_seconds == 20
    assert_full_time(diffuser.modes[0])

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    await hass.services.async_call(
        DOMAIN,
        "set_spray_cycle",
        {"device_id": device.id, "run_seconds": 14, "pause_seconds": 700},
        blocking=True,
    )
    assert cycle(diffuser) == (14, 700)
    assert_full_time(diffuser.modes[0])
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "set_spray_cycle", {"device_id": device.id}, blocking=True)


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
