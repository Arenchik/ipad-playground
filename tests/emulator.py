"""A stand-in for the diffuser's BLE link, built from frames captured on a
real TWRA01B-B2691401B2."""
from __future__ import annotations

import asyncio

from bleak.exc import BleakError

from custom_components.twra_diffuser.twra import (
    LIGHT_CYCLE,
    WorkingMode,
    build_frame,
)

CAPABILITY = bytes.fromhex(
    "110054575241303142423236393134303142320c00443030433545363843434442"
    "fb03000005fd300105fd30012000" + "30" * 32
)
ENQUIRY = bytes.fromhex("0109000105000000000000")
STATE_TEMPLATE = bytearray.fromhex(
    "00000500030000001b00e5e5e500000001550000009f051000f40178030000b0270000741c0200"
)
MODE_1 = WorkingMode.from_bytes(bytes.fromhex("0101550000009f051000f40178030000"))


class FakeDiffuser:
    """Answers frames the way the firmware does."""

    def __init__(self, password: str | None = None) -> None:
        self.password = password
        self.power = True
        self.light = 0
        self.modes = [MODE_1]
        self.written: list[bytes] = []
        self.time_syncs = 0
        self.connections = 0

    def state_payload(self) -> bytes:
        payload = bytearray(STATE_TEMPLATE)
        payload[4] = 0x03 if self.power else 0x02
        payload[10:13] = bytes(LIGHT_CYCLE[self.light])
        return bytes(payload)

    def modes_payload(self) -> bytes:
        return len(self.modes).to_bytes(2, "little") + b"".join(m.to_bytes() for m in self.modes)

    def handle(self, frame: bytes) -> list[bytes]:
        self.written.append(frame)
        command, data = frame[3], frame[4:-2]
        if command == 0x47:
            return [build_frame(0xC7, b"\x01" if self.password else b"\x00")]
        if command == 0x48:
            ok = self.password is not None and data[1:].decode() == self.password
            return [build_frame(0xC8, b"\x01" if ok else b"\x00")]
        if command == 0x09:
            return [build_frame(0x89, ENQUIRY)]
        if command == 0x51:
            return [build_frame(0xD1, CAPABILITY)]
        if command == 0x06:
            self.time_syncs += 1
            return []
        if command == 0x08:
            return [build_frame(0x88, self.modes_payload()), build_frame(0x21, self.state_payload())]
        if command == 0x07:
            action = data[0]
            if action == 0x12:
                self.power = bool(data[1])
            elif action == 0x15:
                self.light = (self.light + 1) % len(LIGHT_CYCLE)
            return [build_frame(0x87, b"\x00"), build_frame(0x21, self.state_payload())]
        if command == 0x14:
            mode = WorkingMode.from_bytes(data)
            self.modes = [mode if m.serial == mode.serial else m for m in self.modes]
            return [build_frame(0x94, b"\x00")]
        return []


class FakeBleakClient:
    def __init__(self, device: FakeDiffuser, disconnected_callback=None) -> None:
        self._device = device
        self._disconnected_callback = disconnected_callback
        self._notify = None
        self.is_connected = True
        device.connections += 1

    async def start_notify(self, uuid, callback) -> None:
        self._notify = callback

    async def write_gatt_char(self, uuid, data, response=False) -> None:
        if not self.is_connected:
            raise BleakError("not connected")
        loop = asyncio.get_running_loop()
        for reply in self._device.handle(bytes(data)):
            # Split long replies like the radio does.
            for i in range(0, len(reply), 20):
                loop.call_soon(self._notify, None, bytearray(reply[i : i + 20]))

    async def disconnect(self) -> None:
        self.is_connected = False

    def drop(self) -> None:
        """Simulate the diffuser going out of range."""
        self.is_connected = False
        if self._disconnected_callback:
            self._disconnected_callback(self)
