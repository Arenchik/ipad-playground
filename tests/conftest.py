"""Fixtures: a fake diffuser behind the real client and BLE plumbing."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .emulator import FakeBleakClient, FakeDiffuser

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "TWRA01B-B2691401B2"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def ble_device() -> BLEDevice:
    return BLEDevice(ADDRESS, NAME, None)


@pytest.fixture
def diffuser() -> FakeDiffuser:
    return FakeDiffuser()


@pytest.fixture
def links(diffuser):
    """Route every connection the client opens to the fake diffuser."""
    opened: list[FakeBleakClient] = []

    async def fake_establish(client_class, device, name, disconnected_callback=None, **kwargs):
        if getattr(diffuser, "out_of_range", False):
            raise BleakError("device out of range")
        link = FakeBleakClient(diffuser, disconnected_callback)
        opened.append(link)
        return link

    with patch(
        "custom_components.twra_diffuser.twra.establish_connection", side_effect=fake_establish
    ):
        yield opened
