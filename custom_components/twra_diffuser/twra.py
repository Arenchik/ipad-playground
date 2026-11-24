"""BLE protocol and client for TWRA (Tersway TW-RA01) aroma diffusers.

The device speaks the YooAI "Scent Tech" framing on a single
characteristic (FFE1, write + notify):

    55 AA LEN CMD DATA... CHK 5A

LEN counts CMD + DATA, CHK makes the byte sum from 0x55 through CHK zero
(mod 256), integers are little-endian and a reply carries the request
command | 0x80. Everything here was verified against a TWRA01B unit.

This module has no Home Assistant imports so it can be driven with plain
bleak from a desktop for debugging.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

HEADER = b"\x55\xaa"
TRAILER = 0x5A

CMD_TIME_SYNC = 0x06
CMD_CONTROL = 0x07
CMD_READ_TIMERS = 0x08
CMD_ENQUIRY = 0x09
CMD_WRITE_TIMER = 0x14
CMD_PW_QUERY = 0x47
CMD_PW_SUBMIT = 0x48
CMD_CAPABILITY = 0x51
RESP_STATE = 0x21
REPLY_FLAG = 0x80

ACTION_POWER = 0x12
# Any value advances the lamp one step through LIGHT_CYCLE; the firmware
# ignores the value byte.
ACTION_LIGHT = 0x15

# Lamp colours as reported in state bytes 10..12, in the order one
# ACTION_LIGHT press steps through them.
LIGHT_CYCLE: tuple[tuple[int, int, int], ...] = (
    (0xE5, 0xE5, 0xE5),
    (0x3D, 0xED, 0xFF),
    (0xE4, 0x82, 0x2A),
    (0x00, 0x00, 0x00),
)
LIGHT_WHITE, LIGHT_CYAN, LIGHT_AMBER, LIGHT_OFF = range(4)

TIMER_RECORD_SIZE = 16
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_ANY = 0x80

REPLY_TIMEOUT = 5.0
STATE_TIMEOUT = 2.0


class TwraError(Exception):
    """Base error for the diffuser client."""


class PasswordRequired(TwraError):
    """The device has an app password and none was configured."""


class InvalidPassword(TwraError):
    """The device rejected the configured password."""


class CommandRejected(TwraError):
    """The device answered a command with a failure code."""


def build_frame(command: int, data: bytes = b"") -> bytes:
    body = HEADER + bytes((len(data) + 1, command)) + data
    return body + bytes(((-sum(body)) & 0xFF, TRAILER))


class FrameParser:
    """Reassembles frames that may be split across notifications."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Return (command, payload) for every complete, valid frame."""
        self._buffer.extend(data)
        frames: list[tuple[int, bytes]] = []
        while True:
            start = self._buffer.find(HEADER)
            if start < 0:
                if self._buffer.endswith(HEADER[:1]):
                    self._buffer[:] = HEADER[:1]
                else:
                    self._buffer.clear()
                return frames
            del self._buffer[:start]
            if len(self._buffer) < 3:
                return frames
            size = self._buffer[2] + 5
            if len(self._buffer) < size:
                return frames
            frame = bytes(self._buffer[:size])
            del self._buffer[:size]
            if frame[-1] != TRAILER or sum(frame[:-1]) & 0xFF:
                _LOGGER.debug("Dropping invalid frame %s", frame.hex())
                continue
            frames.append((frame[3], frame[4:-2]))


@dataclass(frozen=True)
class WorkingMode:
    """One schedule record ("Working Mode" in the app)."""

    enabled: bool
    serial: int
    weekdays: int  # bit0 Mon ... bit6 Sun, bit7 kept as the device sent it
    start_minute: int
    end_minute: int
    run_seconds: int
    pause_seconds: int
    timer_id: int

    @classmethod
    def from_bytes(cls, data: bytes) -> WorkingMode:
        if len(data) != TIMER_RECORD_SIZE:
            raise ValueError(f"expected {TIMER_RECORD_SIZE} bytes, got {len(data)}")
        return cls(
            enabled=data[0] != 0,
            serial=data[1],
            weekdays=int.from_bytes(data[2:4], "little"),
            start_minute=int.from_bytes(data[4:6], "little"),
            end_minute=int.from_bytes(data[6:8], "little"),
            run_seconds=int.from_bytes(data[8:10], "little"),
            pause_seconds=int.from_bytes(data[10:12], "little"),
            timer_id=int.from_bytes(data[12:16], "little"),
        )

    def to_bytes(self) -> bytes:
        return b"".join(
            (
                bytes((int(self.enabled), self.serial & 0xFF)),
                self.weekdays.to_bytes(2, "little"),
                self.start_minute.to_bytes(2, "little"),
                self.end_minute.to_bytes(2, "little"),
                self.run_seconds.to_bytes(2, "little"),
                self.pause_seconds.to_bytes(2, "little"),
                self.timer_id.to_bytes(4, "little"),
            )
        )

    @property
    def days(self) -> list[str]:
        return [day for bit, day in enumerate(WEEKDAYS) if self.weekdays & (1 << bit)]

    def with_days(self, days: list[str]) -> WorkingMode:
        mask = 0
        for day in days:
            mask |= 1 << WEEKDAYS.index(day)
        # Keep the "any day" flag only if this firmware uses it.
        if self.weekdays & WEEKDAY_ANY and mask:
            mask |= WEEKDAY_ANY
        return replace(self, weekdays=mask)

    def with_changes(self, **changes) -> WorkingMode:
        return replace(self, **changes)


def parse_modes(payload: bytes) -> tuple[WorkingMode, ...] | None:
    """0x88 payload: u16 record count, then that many 16-byte records."""
    if len(payload) < 2:
        return None
    count = int.from_bytes(payload[:2], "little")
    if len(payload) < 2 + count * TIMER_RECORD_SIZE:
        return None
    return tuple(
        WorkingMode.from_bytes(payload[2 + i * TIMER_RECORD_SIZE : 2 + (i + 1) * TIMER_RECORD_SIZE])
        for i in range(count)
    )


@dataclass(frozen=True)
class DiffuserState:
    """Pushed state (0x21) plus the last schedule read."""

    power: bool | None = None
    light_rgb: tuple[int, int, int] | None = None
    modes: tuple[WorkingMode, ...] = ()
    raw: bytes = b""

    @property
    def light_index(self) -> int | None:
        if self.light_rgb in LIGHT_CYCLE:
            return LIGHT_CYCLE.index(self.light_rgb)
        return None

    @property
    def light_on(self) -> bool | None:
        if self.light_rgb is None:
            return None
        return self.light_rgb != LIGHT_CYCLE[LIGHT_OFF]


def parse_state(payload: bytes, previous: DiffuserState) -> DiffuserState:
    if len(payload) < 13:
        return previous
    return replace(
        previous,
        power=bool(payload[4] & 0x01),
        light_rgb=(payload[10], payload[11], payload[12]),
        raw=bytes(payload),
    )


@dataclass(frozen=True)
class DeviceIdentity:
    serial: str | None = None
    hardware_id: str | None = None


def parse_capability(payload: bytes) -> DeviceIdentity:
    """0xD1 starts with two u16-length-prefixed ASCII strings."""
    strings: list[str] = []
    pos = 0
    for _ in range(2):
        if pos + 2 > len(payload):
            break
        size = int.from_bytes(payload[pos : pos + 2], "little")
        chunk = payload[pos + 2 : pos + 2 + size]
        if len(chunk) != size:
            break
        strings.append(chunk.decode("ascii", "replace"))
        pos += 2 + size
    return DeviceIdentity(
        serial=strings[0] if strings else None,
        hardware_id=strings[1] if len(strings) > 1 else None,
    )


class TwraClient:
    """Connection to one diffuser.

    With keep_connected the link is held open so commands are instant and
    state is pushed. Otherwise the client drops the link after
    idle_timeout seconds so the phone app can still connect (the device
    accepts a single connection).
    """

    def __init__(
        self,
        ble_device: BLEDevice,
        password: str | None = None,
        keep_connected: bool = True,
        idle_timeout: float = 30.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._password = password
        self._keep_connected = keep_connected
        self._idle_timeout = idle_timeout
        self._now = now or (lambda: datetime.now().astimezone())
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._parser = FrameParser()
        self._waiters: dict[int, list[asyncio.Future[bytes]]] = {}
        self._callbacks: list[Callable[[], None]] = []
        self._idle_handle: asyncio.TimerHandle | None = None
        self._disconnect_task: asyncio.Task | None = None
        self.state = DiffuserState()
        self.identity = DeviceIdentity()

    # -- properties --------------------------------------------------------

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def name(self) -> str:
        return self._ble_device.name or self._ble_device.address

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def keep_connected(self) -> bool:
        return self._keep_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device

    def restore(self, state: DiffuserState, identity: DeviceIdentity) -> None:
        """Seed the last known state, e.g. from disk before a first connect."""
        self.state = state
        self.identity = identity

    def notify(self) -> None:
        """Tell listeners to re-read; for changes outside the client."""
        self._notify()

    def register_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback` on every state or connection change."""
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._callbacks):
            callback()

    # -- public operations -------------------------------------------------

    async def connect(self) -> None:
        await self._run(self._noop)

    async def refresh(self) -> None:
        """Re-read the schedule; the device pushes its state right after."""
        await self._run(lambda: self._request(CMD_READ_TIMERS, expect_state=True))

    async def sync_time(self) -> None:
        await self._run(self._send_time)

    async def set_power(self, on: bool) -> None:
        async def op() -> None:
            await self._control(ACTION_POWER, 1 if on else 0)
            if self.state.power != on:
                self.state = replace(self.state, power=on)
                self._notify()

        await self._run(op)

    async def set_light(self, index: int) -> None:
        """Step the lamp until it shows LIGHT_CYCLE[index]."""
        if not 0 <= index < len(LIGHT_CYCLE):
            raise ValueError(f"light index out of range: {index}")

        async def op() -> None:
            for _ in range(len(LIGHT_CYCLE) + 2):
                if self.state.light_index == index:
                    return
                await self._control(ACTION_LIGHT, 1)
            raise TwraError(f"lamp did not reach colour {index}")

        await self._run(op)

    async def update_mode(self, mode: WorkingMode) -> None:
        async def op() -> None:
            reply = await self._request(CMD_WRITE_TIMER, mode.to_bytes())
            if reply[:1] != b"\x00":
                raise CommandRejected(f"mode write rejected: {reply.hex()}")
            await self._request(CMD_READ_TIMERS, expect_state=True)

        await self._run(op)

    async def disconnect(self) -> None:
        self._cancel_idle()
        async with self._lock:
            await self._disconnect_locked()

    # -- connection handling -----------------------------------------------

    async def _noop(self) -> None:
        return None

    async def _run(self, operation: Callable[[], object]) -> None:
        self._cancel_idle()
        async with self._lock:
            try:
                for attempt in (1, 2):
                    await self._ensure_connected()
                    try:
                        await operation()
                        break
                    except (BleakError, TimeoutError):
                        # A dropped link surfaces here; reconnect once.
                        if attempt == 2 or self.connected:
                            raise
                        _LOGGER.debug("%s: link lost during command, retrying", self.name)
            finally:
                self._schedule_idle()

    async def _ensure_connected(self) -> None:
        if self.connected:
            return
        _LOGGER.debug("%s: connecting", self.name)
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnected,
            max_attempts=3,
            ble_device_callback=lambda: self._ble_device,
        )
        self._parser.reset()
        self._client = client
        try:
            await client.start_notify(CHAR_UUID, self._on_notification)
            await self._open_session()
        except BaseException:
            await self._disconnect_locked()
            raise
        self._notify()

    async def _open_session(self) -> None:
        """Same order as the vendor app: password, enquiry, identity, clock."""
        reply = await self._request(CMD_PW_QUERY)
        if reply[:1] not in (b"", b"\x00"):
            if not self._password:
                raise PasswordRequired
            secret = self._password.encode("ascii", "replace")[:4]
            reply = await self._request(CMD_PW_SUBMIT, bytes((len(secret),)) + secret)
            if reply[:1] in (b"", b"\x00"):
                raise InvalidPassword
        await self._request(CMD_ENQUIRY, b"\x01\x00")
        self.identity = parse_capability(await self._request(CMD_CAPABILITY))
        await self._send_time()
        await self._request(CMD_READ_TIMERS, expect_state=True)

    async def _send_time(self) -> None:
        # The firmware compares its schedule against local wall-clock time
        # encoded as if it were a UTC epoch, which is what the app sends.
        wall = self._now().replace(tzinfo=timezone.utc)
        timestamp = int(wall.timestamp()) & 0xFFFFFFFF
        await self._write(build_frame(CMD_TIME_SYNC, timestamp.to_bytes(4, "little")))
        await asyncio.sleep(0.2)

    async def _disconnect_locked(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except BleakError as err:
                _LOGGER.debug("%s: error while disconnecting: %s", self.name, err)
        self._fail_waiters(BleakError("disconnected"))
        self._notify()

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        if client is not self._client:
            return
        _LOGGER.debug("%s: disconnected", self.name)
        self._client = None
        self._fail_waiters(BleakError("disconnected"))
        self._notify()

    def _schedule_idle(self) -> None:
        if self._keep_connected or not self.connected:
            return
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(self._idle_timeout, self._idle_expired)

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _idle_expired(self) -> None:
        self._idle_handle = None
        self._disconnect_task = asyncio.get_running_loop().create_task(self.disconnect())

    # -- framing -----------------------------------------------------------

    async def _write(self, frame: bytes) -> None:
        if self._client is None:
            raise BleakError("not connected")
        _LOGGER.debug("%s: -> %s", self.name, frame.hex())
        await self._client.write_gatt_char(CHAR_UUID, frame, response=False)

    async def _control(self, action: int, value: int) -> None:
        reply = await self._request(CMD_CONTROL, bytes((action, value, 0)), expect_state=True)
        if reply[:1] != b"\x00":
            raise CommandRejected(f"control {action:#04x} rejected: {reply.hex()}")

    async def _request(self, command: int, data: bytes = b"", expect_state: bool = False) -> bytes:
        """Send a frame and return the payload of its reply.

        With expect_state, also wait briefly for the state push that
        follows controls and schedule reads.
        """
        loop = asyncio.get_running_loop()
        reply = loop.create_future()
        self._waiters.setdefault(command | REPLY_FLAG, []).append(reply)
        state = None
        if expect_state:
            state = loop.create_future()
            self._waiters.setdefault(RESP_STATE, []).append(state)
        try:
            await self._write(build_frame(command, data))
            async with asyncio.timeout(REPLY_TIMEOUT):
                payload = await reply
            if state is not None:
                try:
                    async with asyncio.timeout(STATE_TIMEOUT):
                        await state
                except TimeoutError:
                    _LOGGER.debug("%s: no state push after %#04x", self.name, command)
            return payload
        finally:
            for future in (reply, state):
                if future is None:
                    continue
                for waiters in self._waiters.values():
                    if future in waiters:
                        waiters.remove(future)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    # Failed by a dropped link while nobody was awaiting it.
                    future.exception()

    def _fail_waiters(self, err: Exception) -> None:
        for waiters in self._waiters.values():
            for future in waiters:
                if not future.done():
                    future.set_exception(err)
        self._waiters.clear()

    def _on_notification(self, _sender: object, data: bytearray) -> None:
        _LOGGER.debug("%s: <- %s", self.name, bytes(data).hex())
        changed = False
        for command, payload in self._parser.feed(bytes(data)):
            if command == RESP_STATE:
                new = parse_state(payload, self.state)
                changed |= new != self.state
                self.state = new
            elif command == CMD_READ_TIMERS | REPLY_FLAG:
                modes = parse_modes(payload)
                if modes is not None and modes != self.state.modes:
                    self.state = replace(self.state, modes=modes)
                    changed = True
            for future in self._waiters.get(command, ()):
                if not future.done():
                    future.set_result(payload)
                    break
        if changed:
            self._notify()
