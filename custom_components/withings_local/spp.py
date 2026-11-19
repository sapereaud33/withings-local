"""Outgoing Classic RFCOMM (Serial Port Profile) to a Body+ (WBS05), through BlueZ.

Hardware findings on one Body+ (2026-11-19, HA OS 18.4, BlueZ 5.86):
- it never sends a connectable LE advertisement, neither in setup mode nor after a weigh-in
  (only ADV_SCAN_IND), so GATT as documented by openwithings is impossible on this firmware;
- it accepts a Classic connection while awake and publishes SDP records for SPP (0x1101), Apple
  iAP2 (00000000-deca-fade-deca-deafdecacaff) and the Withings service (…5749-5448-0005-…).

So, like the phone app on Android, Home Assistant connects to the scale's SPP channel. BlueZ
does the SDP lookup and RFCOMM connect: we register a client Profile1 for the SPP UUID (no
auto-connect), call Device1.ConnectProfile, and receive the socket in NewConnection.
"""

import asyncio
import logging
import os
import secrets
import socket

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
CONNECT_TIMEOUT = 25
# The scale answers the first page right after a weigh-in with "Connection Rejected due to
# Limited Resources" (HCI 0x0d, seen 2026-11-19) while it keeps advertising for ~13 s, so keep
# paging it for a while instead of giving up on the first refusal.
RETRY_WINDOW = 30
RETRY_DELAY = 1.0


class SppUnavailable(Exception):
    """BlueZ couldn't open the scale's serial port."""


class _ClientProfile(ServiceInterface):
    def __init__(self, device_path: str) -> None:
        super().__init__("org.bluez.Profile1")
        self.device_path = device_path
        self.sock: asyncio.Future[socket.socket] = asyncio.get_running_loop().create_future()

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):  # noqa: N802,F821,F722
        if device != self.device_path or self.sock.done():
            os.close(fd)
            return
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        self.sock.set_result(sock)

    @method()
    def RequestDisconnection(self, device: "o"):  # noqa: N802,F821
        return

    @method()
    def Release(self):  # noqa: N802
        return


async def _call(bus: MessageBus, path: str, interface: str, member: str, signature: str = "", body=None,
                timeout: float = 10) -> Message:
    reply = await asyncio.wait_for(
        bus.call(Message(destination=BLUEZ, path=path, interface=interface, member=member,
                         signature=signature, body=body or [])),
        timeout=timeout,
    )
    if reply.message_type == MessageType.ERROR:
        detail = reply.body[0] if reply.body else ""
        raise SppUnavailable(f"{interface}.{member}: {reply.error_name} {detail}".strip())
    return reply


async def open_spp(device_path: str, retry_window: float = RETRY_WINDOW) -> socket.socket:
    """Connect to the scale's Serial Port, retrying while it refuses, and return the socket."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + retry_window
    attempt = 0
    while True:
        attempt += 1
        try:
            sock = await _open_spp_once(device_path)
        except SppUnavailable as err:
            if loop.time() + RETRY_DELAY >= deadline:
                raise SppUnavailable(f"{err} (after {attempt} attempts)") from err
            _LOGGER.debug("SPP attempt %d failed: %s; retrying", attempt, err)
            await asyncio.sleep(RETRY_DELAY)
            continue
        if attempt > 1:
            _LOGGER.debug("SPP connected on attempt %d", attempt)
        return sock


async def _open_spp_once(device_path: str) -> socket.socket:
    """Connect to the scale's Serial Port and return the (non-blocking) RFCOMM socket.

    The caller owns and closes the socket. The client profile is unregistered before returning,
    which doesn't affect the open socket.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    path = f"/org/home_assistant/withings_local/spp_{secrets.token_hex(4)}"
    profile = _ClientProfile(device_path)
    bus.export(path, profile)
    registered = False
    try:
        await _call(bus, "/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile", "osa{sv}", [
            path, SPP_UUID, {
                "Name": Variant("s", "Withings scale"),
                "Role": Variant("s", "client"),
                "AutoConnect": Variant("b", False),
                "RequireAuthentication": Variant("b", False),
                "RequireAuthorization": Variant("b", False),
            },
        ])
        registered = True
        connect = asyncio.ensure_future(_call(
            bus, device_path, "org.bluez.Device1", "ConnectProfile", "s", [SPP_UUID], timeout=CONNECT_TIMEOUT
        ))
        done, _ = await asyncio.wait({connect, profile.sock}, timeout=CONNECT_TIMEOUT,
                                     return_when=asyncio.FIRST_COMPLETED)
        if profile.sock.done():
            if not connect.done():
                connect.cancel()
            return profile.sock.result()
        if connect in done:
            connect.result()  # raises SppUnavailable on a BlueZ error
            # ConnectProfile returned before NewConnection arrived: give it a moment.
            async with asyncio.timeout(3):
                return await asyncio.shield(profile.sock)
        connect.cancel()
        raise SppUnavailable(f"no RFCOMM connection within {CONNECT_TIMEOUT}s")
    except TimeoutError as err:
        raise SppUnavailable("BlueZ didn't hand over the RFCOMM socket") from err
    finally:
        if registered:
            try:
                await _call(bus, "/org/bluez", "org.bluez.ProfileManager1", "UnregisterProfile", "o", [path])
            except Exception:  # noqa: BLE001
                _LOGGER.debug("could not unregister the SPP client profile", exc_info=True)
        bus.unexport(path, profile)
        bus.disconnect()
