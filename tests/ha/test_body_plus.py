"""Body+ (WBS05): discovery, weigh-in detection and the Just Works pairing step."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.withings_local.config_flow import scale_identity_mac, scale_model_id
from custom_components.withings_local.const import BODY_PLUS_MODEL_ID, CONF_KL, CONF_MODEL_ID, DOMAIN
from custom_components.withings_local.coordinator import has_new_data, is_setup_mode

from .conftest import TEST_KL, service_info

# Synthetic values in the shape of a real Body+ advertisement (no local name, Withings
# manufacturer data, the setup-mode UUID merged with the weigh-in one).
ADDRESS = "00:24:E4:00:00:2B"
SETUP = "00000020-5749-5448-0005-000000000000"
INSTALLED = "00000020-5749-5448-0005-0024e400002a"


def body_plus(uuid=INSTALLED, mfr=b"\xbc\x25\x2b\xc0", name=None):
    return service_info(name=name, address=ADDRESS, uuid=uuid, mfr=mfr)


def test_recognized_and_identity_mac():
    assert scale_model_id(body_plus()) == BODY_PLUS_MODEL_ID
    assert scale_model_id(body_plus(uuid=None, name="Body+ 2B")) == BODY_PLUS_MODEL_ID
    assert scale_identity_mac(body_plus(), BODY_PLUS_MODEL_ID) == "00:24:E4:00:00:2A"


def test_weigh_in_and_setup_mode():
    assert has_new_data(body_plus(), BODY_PLUS_MODEL_ID)
    # Manufacturer data alone isn't trusted for the Body+.
    assert not has_new_data(body_plus(uuid=SETUP), BODY_PLUS_MODEL_ID)
    assert not has_new_data(body_plus(uuid=None), BODY_PLUS_MODEL_ID)
    assert is_setup_mode(body_plus(uuid=SETUP), BODY_PLUS_MODEL_ID)
    assert not is_setup_mode(body_plus(), BODY_PLUS_MODEL_ID)


async def test_discovery_pairs_with_just_works(hass: HomeAssistant, mock_bluetooth) -> None:
    def fake_register(hass_, callback, address):
        setup = body_plus(uuid=SETUP)
        setup.time = 1e12
        hass.loop.call_soon(callback, setup)
        return lambda: None

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "bluetooth"}, data=body_plus()
    )
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"]["name"] == "Body+"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KL: TEST_KL})
    assert result["step_id"] == "pair"
    assert "Start app" in result["description_placeholders"]["pairing_instructions"]

    pairing = AsyncMock(return_value=False)
    with (
        patch("custom_components.withings_local.config_flow.bluetooth.async_register_advertisement_callback", fake_register),
        patch("custom_components.withings_local.config_flow.device_path", return_value="/org/bluez/hci0/dev_00_24_E4_00_00_2B"),
        patch("custom_components.withings_local.config_flow.BodyPlusPairing") as factory,
        patch("custom_components.withings_local.async_setup_entry", return_value=True),
    ):
        factory.return_value.pair = pairing
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_now"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "wait_for_scale_body_plus"
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"address": ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_PLUS_MODEL_ID}
    pairing.assert_awaited_once()
