"""Protocol and client tests, using frames captured from a real unit."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.twra_diffuser.twra import (
    LIGHT_AMBER,
    LIGHT_CYAN,
    LIGHT_OFF,
    LIGHT_WHITE,
    DiffuserState,
    FrameParser,
    InvalidPassword,
    PasswordRequired,
    TwraClient,
    build_frame,
    parse_capability,
    parse_modes,
    parse_state,
)

from .emulator import CAPABILITY, FakeDiffuser


def test_build_frame_matches_vendor_app() -> None:
    assert build_frame(0x07, bytes((0x12, 1, 0))).hex() == "55aa0407120100e35a"
    assert build_frame(0x07, bytes((0x12, 0, 0))).hex() == "55aa0407120000e45a"
    assert build_frame(0x08).hex() == "55aa0108f85a"
    assert build_frame(0x47).hex() == "55aa0147b95a"


def test_parser_reassembles_split_and_drops_corrupt() -> None:
    state = bytes.fromhex(
        "55aa282100000500030000001b00e5e5e500000001550000009f051000f40178030000b0270000741c0200035a"
    )
    parser = FrameParser()
    assert parser.feed(b"\x00\x55") == []
    assert parser.feed(state[1:20]) == []
    frames = parser.feed(state[20:] + bytes.fromhex("55aa02c700395a"))  # bad checksum
    assert [cmd for cmd, _ in frames] == [0x21]
    parsed = parse_state(frames[0][1], DiffuserState())
    assert parsed.power is True
    assert parsed.light_index == LIGHT_WHITE


def test_parse_modes_matches_app_screen() -> None:
    (mode,) = parse_modes(bytes.fromhex("01000101550000009f051000f40178030000"))
    assert mode.enabled and mode.serial == 1
    assert mode.days == ["mon", "wed", "fri", "sun"]
    assert (mode.start_minute, mode.end_minute) == (0, 23 * 60 + 59)
    assert (mode.run_seconds, mode.pause_seconds) == (16, 500)
    assert mode.with_days(["mon", "wed", "fri", "sun"]) == mode
    assert mode.with_days(["tue"]).weekdays == 0x02
    assert parse_modes(bytes.fromhex("0200")) is None


def test_parse_capability() -> None:
    identity = parse_capability(CAPABILITY)
    assert identity.serial == "TWRA01BB2691401B2"
    assert identity.hardware_id == "D00C5E68CCDB"


async def test_session_and_commands(ble_device, diffuser, links) -> None:
    moscow = timezone(timedelta(hours=3))
    client = TwraClient(ble_device, now=lambda: datetime(2026, 11, 20, 20, 0, tzinfo=moscow))
    updates = []
    client.register_callback(lambda: updates.append(1))

    await client.connect()
    assert client.connected
    assert client.identity.serial == "TWRA01BB2691401B2"
    assert client.state.power is True
    assert client.state.modes[0].pause_seconds == 500
    # Local wall clock sent as if it were UTC.
    sync = next(f for f in diffuser.written if f[3] == 0x06)
    sent = int.from_bytes(sync[4:8], "little")
    assert sent == int(datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc).timestamp())

    for target in (LIGHT_AMBER, LIGHT_OFF, LIGHT_WHITE, LIGHT_CYAN):
        await client.set_light(target)
        assert diffuser.light == target == client.state.light_index

    await client.set_power(False)
    assert diffuser.power is False and client.state.power is False

    mode = client.state.modes[0]
    await client.update_mode(mode.with_changes(enabled=False, pause_seconds=120))
    assert diffuser.modes[0].enabled is False
    assert client.state.modes[0].pause_seconds == 120
    assert updates

    await client.disconnect()
    assert not client.connected


async def test_reconnects_once_when_link_drops(ble_device, diffuser, links) -> None:
    client = TwraClient(ble_device)
    await client.connect()
    links[0].drop()
    assert not client.connected
    await client.set_power(False)
    assert len(links) == 2 and diffuser.power is False


async def test_idle_disconnect(ble_device, diffuser, links) -> None:
    client = TwraClient(ble_device, keep_connected=False, idle_timeout=0.05)
    await client.refresh()
    assert client.connected
    await asyncio.sleep(0.2)
    assert not client.connected
    await client.set_power(False)
    assert diffuser.power is False and len(links) == 2
    await client.disconnect()


async def test_password(ble_device, links, diffuser) -> None:
    diffuser.password = "1234"
    with pytest.raises(PasswordRequired):
        await TwraClient(ble_device).connect()
    with pytest.raises(InvalidPassword):
        await TwraClient(ble_device, password="0000").connect()
    client = TwraClient(ble_device, password="1234")
    await client.connect()
    assert client.connected
