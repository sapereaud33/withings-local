"""One-time Just Works pairing with a Body+ (WBS05), through BlueZ.

The Body+ refuses the subscription to its WPP characteristic with "Insufficient Authentication"
until the link is encrypted, and only completes pairing when the host elevates security in
reaction to that error. Pairing proactively with Device1.Pair() makes it go silent after the
SMP random exchange (openwithings, docs/bluez-pitfalls.md #5). So instead of Pair(), this:

1. registers a NoInputNoOutput agent that confirms Just Works for this scale only (Home
   Assistant's BlueZ has no agent, and without one BlueZ rejects the pairing),
2. makes the adapter bondable for the attempt (the kernel only asks for a bond when it is),
3. connects and runs a setup-mode WPP session; the subscription triggers the pairing,
4. restores the adapter settings and checks that BlueZ stored the bond.

The supervision-timeout problem openwithings hit on a Raspberry Pi (pitfalls #6, #7) doesn't
apply: Home Assistant loads 10 s / 8 s connection parameters for every connection it makes.
"""

import asyncio
import logging
import secrets

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant

from .classic_pairing import ClassicAgent
from .pairing import PairingFailed
from .withings_wpp.client import WppSession, setup_session
from .withings_wpp.devices import BODY_PLUS

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROPERTIES = "org.freedesktop.DBus.Properties"
AGENT_MANAGER = "org.bluez.AgentManager1"
SESSION_TIMEOUT = 45
PAIRABLE_FAILSAFE_SECONDS = SESSION_TIMEOUT + 30


class BodyPlusPairing:
    """One pairing attempt with a Body+ in setup mode."""

    def __init__(self, hass: HomeAssistant, device_path: str, service_info: BluetoothServiceInfoBleak) -> None:
        self.hass = hass
        self.device_path = device_path
        self.adapter_path = device_path.split("/dev_", 1)[0]
        self.service_info = service_info
        self.agent_path = f"/org/home_assistant/withings_local/le_{secrets.token_hex(5)}"

    async def _call(self, bus: MessageBus, path: str, interface: str, member: str, signature="", body=None):
        reply = await asyncio.wait_for(
            bus.call(Message(destination=BLUEZ, path=path, interface=interface, member=member,
                             signature=signature, body=body or [])),
            timeout=8,
        )
        if reply.message_type == MessageType.ERROR:
            raise PairingFailed(f"BlueZ {interface}.{member} failed ({reply.error_name})")
        return reply

    async def _get(self, bus: MessageBus, path: str, interface: str, name: str):
        return (await self._call(bus, path, PROPERTIES, "Get", "ss", [interface, name])).body[0].value

    async def _set(self, bus: MessageBus, path: str, interface: str, name: str, value: int | bool) -> None:
        kind = "b" if isinstance(value, bool) else "u"
        await self._call(bus, path, PROPERTIES, "Set", "ssv", [interface, name, Variant(kind, value)])

    async def _paired(self, bus: MessageBus) -> bool:
        try:
            return await self._get(bus, self.device_path, DEVICE, "Paired") is True
        except PairingFailed:
            return False  # BlueZ has no object for the scale yet

    async def pair(self) -> bool:
        """Return True if the scale was already paired, False after pairing it now."""
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=False).connect()
        agent = ClassicAgent(self.device_path)
        exported = registered = False
        pairable_before: bool | None = None
        timeout_before: int | None = None
        cleanup_errors: list[str] = []
        try:
            if await self._paired(bus):
                return True
            bus.export(self.agent_path, agent)
            exported = True
            await self._call(bus, "/org/bluez", AGENT_MANAGER, "RegisterAgent", "os",
                             [self.agent_path, "NoInputNoOutput"])
            registered = True
            await self._call(bus, "/org/bluez", AGENT_MANAGER, "RequestDefaultAgent", "o", [self.agent_path])
            pairable_before = await self._get(bus, self.adapter_path, ADAPTER, "Pairable")
            if pairable_before is not True:
                timeout_before = await self._get(bus, self.adapter_path, ADAPTER, "PairableTimeout")
                await self._set(bus, self.adapter_path, ADAPTER, "PairableTimeout", PAIRABLE_FAILSAFE_SECONDS)
                await self._set(bus, self.adapter_path, ADAPTER, "Pairable", True)
            await self._run_setup_session()
            for _ in range(20):
                if await self._paired(bus):
                    break
                await asyncio.sleep(0.25)
            else:
                raise PairingFailed("the session ended but BlueZ has no bond for the scale")
            try:
                await self._set(bus, self.device_path, DEVICE, "Trusted", True)
            except PairingFailed:
                _LOGGER.debug("could not mark the scale trusted", exc_info=True)
            return False
        finally:
            if pairable_before is False:
                for name, value in (("Pairable", False), ("PairableTimeout", timeout_before)):
                    if value is None:
                        continue
                    try:
                        await self._set(bus, self.adapter_path, ADAPTER, name, value)
                    except Exception:  # noqa: BLE001 - restore the rest regardless
                        cleanup_errors.append(name)
            if registered:
                try:
                    await self._call(bus, "/org/bluez", AGENT_MANAGER, "UnregisterAgent", "o", [self.agent_path])
                except Exception:  # noqa: BLE001
                    cleanup_errors.append("agent")
            if exported:
                bus.unexport(self.agent_path, agent)
            bus.disconnect()
            if cleanup_errors:
                _LOGGER.error("Body+ pairing cleanup failed for %s", ",".join(cleanup_errors))

    async def _run_setup_session(self) -> None:
        address = self.service_info.address
        device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True) or self.service_info.device
        session: WppSession | None = None

        def on_disconnect(_client) -> None:
            if session is not None:
                session.mark_disconnected()

        client = await establish_connection(
            BleakClientWithServiceCache, device, "Body+", disconnected_callback=on_disconnect, max_attempts=3,
            ble_device_callback=lambda: bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
            or device,
        )
        try:
            session = WppSession(client, profile=BODY_PLUS)
            async with asyncio.timeout(SESSION_TIMEOUT):
                reply = await setup_session(session)
            if reply is None:
                _LOGGER.info("Body+ asked for authentication (not in setup mode); the bond formed anyway")
            else:
                _LOGGER.info("Body+ setup-mode session done (firmware %s)", reply.soft_version)
        except Exception as err:
            raise PairingFailed(f"setup session failed: {type(err).__name__}: {err}") from err
        finally:
            await client.disconnect()
