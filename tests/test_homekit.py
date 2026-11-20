"""End to end through HomeKit Bridge: Home app action -> HA -> diffuser."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .test_init import DIFFUSER, LAMP, MODES, SPRAY, bluetooth, setup_entry  # noqa: F401

pytest.importorskip("pyhap")

from homeassistant.components.homekit.accessories import HomeDriver, get_accessory  # noqa: E402
from homeassistant.components.homekit.iidmanager import AccessoryIIDStorage  # noqa: E402
from homeassistant.components.homekit.util import state_needs_accessory_mode  # noqa: E402
from pyhap.const import (  # noqa: E402
    CATEGORY_HUMIDIFIER,
    CATEGORY_LIGHTBULB,
    CATEGORY_TELEVISION,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_VALUE,
)


@pytest.fixture
async def hk_driver(hass: HomeAssistant):
    storage = AccessoryIIDStorage(hass, "")
    await storage.async_initialize()
    with (
        patch("pyhap.accessory_driver.AsyncZeroconf"),
        patch("pyhap.accessory_driver.AccessoryEncoder"),
        patch("pyhap.accessory_driver.HAPServer.async_stop"),
        patch("pyhap.accessory_driver.HAPServer.async_start"),
        patch("pyhap.accessory_driver.AccessoryDriver.publish"),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
    ):
        yield HomeDriver(
            hass,
            pincode=b"123-45-678",
            entry_id="",
            entry_title="mock entry",
            bridge_name="HASS Bridge",
            iid_storage=storage,
            address="127.0.0.1",
            loop=hass.loop,
        )


async def _set(hass, driver, acc, char, value) -> None:
    driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: acc.aid, HAP_REPR_IID: char.to_HAP()[HAP_REPR_IID], HAP_REPR_VALUE: value}
            ]
        },
        "mock_addr",
    )
    await _settle(hass)


async def _settle(hass) -> None:
    # The Light accessory coalesces characteristic writes for a moment.
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()


async def test_home_app_controls_diffuser(hass, bluetooth, links, diffuser, hk_driver) -> None:  # noqa: F811
    await setup_entry(hass)

    tile = get_accessory(hass, hk_driver, hass.states.get(DIFFUSER), 1, {})
    assert type(tile).__name__ == "HumidifierDehumidifier"
    assert tile.category == CATEGORY_HUMIDIFIER
    hk_driver.add_accessory(tile)
    tile.run()
    await hass.async_block_till_done()
    assert tile.char_active.value == 1
    # The tile's big number and its slider both show the intensity.
    assert tile.char_current_humidity.value == tile.char_target_humidity.value == 18

    await _set(hass, hk_driver, tile, tile.char_target_humidity, 100)
    assert diffuser.modes[0].pause_seconds == 30
    assert tile.char_target_humidity.value == 100
    await _set(hass, hk_driver, tile, tile.char_active, 0)
    assert diffuser.power is False
    assert tile.char_active.value == 0

    spray = get_accessory(hass, hk_driver, hass.states.get(SPRAY), 1, {})
    assert type(spray).__name__ == "SelectSwitch"
    assert list(spray.select_chars) == ["5 s", "10 s", "16 s", "20 s", "30 s", "45 s", "60 s"]
    hk_driver.add_accessory(spray)
    spray.run()
    await hass.async_block_till_done()
    assert spray.select_chars["16 s"].value is True
    await _set(hass, hk_driver, spray, spray.select_chars["30 s"], True)
    assert diffuser.modes[0].run_seconds == 30
    assert spray.select_chars["30 s"].value is True
    assert spray.select_chars["16 s"].value is False

    lamp = get_accessory(hass, hk_driver, hass.states.get(LAMP), 1, {})
    assert type(lamp).__name__ == "Light" and lamp.category == CATEGORY_LIGHTBULB
    hk_driver.add_accessory(lamp)
    lamp.run()
    await hass.async_block_till_done()
    # Pick blue on the Home app colour wheel: hue and saturation arrive
    # together and the lamp snaps to cyan.
    hk_driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: lamp.aid, HAP_REPR_IID: lamp.char_hue.to_HAP()[HAP_REPR_IID], HAP_REPR_VALUE: 220},
                {
                    HAP_REPR_AID: lamp.aid,
                    HAP_REPR_IID: lamp.char_saturation.to_HAP()[HAP_REPR_IID],
                    HAP_REPR_VALUE: 90,
                },
            ]
        },
        "mock_addr",
    )
    await _settle(hass)
    assert diffuser.light == 1
    await _set(hass, hk_driver, lamp, lamp.char_on, 0)
    assert diffuser.light == 3


async def test_home_app_scent_mode_list(hass, bluetooth, links, diffuser, hk_driver) -> None:  # noqa: F811
    await setup_entry(hass)
    state = hass.states.get(MODES)
    # A TV-style accessory: paired on its own, the bridge sets that up.
    assert state_needs_accessory_mode(state)

    tv = get_accessory(hass, hk_driver, state, 1, {})
    assert type(tv).__name__ == "ActivityRemote" and tv.category == CATEGORY_TELEVISION
    assert tv.sources == state.attributes["activity_list"]
    hk_driver.add_accessory(tv)
    tv.run()
    await hass.async_block_till_done()
    assert tv.char_active.value == 1
    assert tv.sources[tv.char_input_source.value] == "Custom settings"

    rich = tv.sources.index("Rich — 20 s, 5 min pause")
    await _set(hass, hk_driver, tv, tv.char_input_source, rich)
    assert (diffuser.modes[0].run_seconds, diffuser.modes[0].pause_seconds) == (20, 300)
    assert tv.char_input_source.value == rich

    await _set(hass, hk_driver, tv, tv.char_active, 0)
    assert diffuser.power is False
