"""The tests for google-assistant init."""

from http import HTTPStatus
from unittest.mock import patch

import pytest

from homeassistant.components import google_assistant as ga
from homeassistant.components.google_assistant import (
    DOMAIN,
    GOOGLE_ASSISTANT_SCHEMA,
    GoogleConfig,
    smart_home as sh,
)
from homeassistant.const import SERVICE_RELOAD
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .test_http import DUMMY_CONFIG

from tests.common import MockConfigEntry
from tests.test_util.aiohttp import AiohttpClientMocker


async def test_import(hass: HomeAssistant) -> None:
    """Test import."""

    await async_setup_component(
        hass,
        ga.DOMAIN,
        {"google_assistant": DUMMY_CONFIG},
    )

    entries = hass.config_entries.async_entries("google_assistant")
    assert len(entries) == 1
    assert entries[0].data[ga.const.CONF_PROJECT_ID] == "1234"


async def test_import_changed(hass: HomeAssistant) -> None:
    """Test import with changed project id."""

    old_entry = MockConfigEntry(
        domain=ga.DOMAIN, data={ga.const.CONF_PROJECT_ID: "4321"}, source="import"
    )
    old_entry.add_to_hass(hass)

    await async_setup_component(
        hass,
        ga.DOMAIN,
        {"google_assistant": DUMMY_CONFIG},
    )
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries("google_assistant")
    assert len(entries) == 1
    assert entries[0].data[ga.const.CONF_PROJECT_ID] == "1234"


async def test_request_sync_service(
    aioclient_mock: AiohttpClientMocker, hass: HomeAssistant
) -> None:
    """Test that it posts to the request_sync url."""
    aioclient_mock.post(
        ga.const.HOMEGRAPH_TOKEN_URL,
        status=HTTPStatus.OK,
        json={"access_token": "1234", "expires_in": 3600},
    )

    aioclient_mock.post(ga.const.REQUEST_SYNC_BASE_URL, status=HTTPStatus.OK)

    await async_setup_component(
        hass,
        DOMAIN,
        {"google_assistant": DUMMY_CONFIG},
    )

    assert aioclient_mock.call_count == 0
    await hass.services.async_call(
        ga.const.DOMAIN,
        ga.const.SERVICE_REQUEST_SYNC,
        blocking=True,
        context=Context(user_id="123"),
    )

    assert aioclient_mock.call_count == 2  # token + request


def _yaml_config(**config) -> dict:
    """Return a validated YAML configuration based on the dummy config."""
    return {DOMAIN: GOOGLE_ASSISTANT_SCHEMA({**DUMMY_CONFIG, **config})}


async def _async_setup(hass: HomeAssistant) -> GoogleConfig:
    """Set up Google Assistant from YAML and return its config."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: DUMMY_CONFIG})
    await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data


async def test_reload_service(hass: HomeAssistant) -> None:
    """Test reloading the YAML configuration applies it and syncs to Google."""
    google_config = await _async_setup(hass)
    assert google_config.entity_config == {}

    with (
        patch(
            "homeassistant.components.google_assistant.http.async_integration_yaml_config",
            return_value=_yaml_config(
                entity_config={"light.kitchen": {"name": "Kitchen", "expose": False}},
                exposed_domains=["light"],
            ),
        ),
        patch.object(GoogleConfig, "async_sync_entities_all") as mock_sync,
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, blocking=True)

    assert google_config.entity_config == {
        "light.kitchen": {"name": "Kitchen", "expose": False}
    }
    assert not google_config.should_expose("light.kitchen")
    assert not google_config.should_expose("switch.outlet")
    assert hass.data[DOMAIN][ga.const.DATA_CONFIG]["exposed_domains"] == ["light"]
    mock_sync.assert_called_once_with()


async def test_reload_service_invalid_config(hass: HomeAssistant) -> None:
    """Test an invalid YAML configuration keeps the current one."""
    google_config = await _async_setup(hass)

    with (
        patch(
            "homeassistant.components.google_assistant.http.async_integration_yaml_config",
            return_value=None,
        ),
        patch.object(GoogleConfig, "async_sync_entities_all"),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, blocking=True)

    assert google_config.entity_config == {}


async def test_reload_service_removed_from_yaml(hass: HomeAssistant) -> None:
    """Test removing Google Assistant from YAML stops exposing entities."""
    google_config = await _async_setup(hass)
    assert google_config.should_expose("light.kitchen")

    with (
        patch(
            "homeassistant.components.google_assistant.http.async_integration_yaml_config",
            return_value={},
        ),
        patch.object(GoogleConfig, "async_sync_entities_all") as mock_sync,
    ):
        await hass.services.async_call(DOMAIN, SERVICE_RELOAD, blocking=True)

    assert not google_config.should_expose("light.kitchen")
    assert not google_config.should_report_state
    mock_sync.assert_called_once_with()


async def test_sync_request_reloads_yaml(hass: HomeAssistant) -> None:
    """Test a SYNC request from Google answers with the current YAML config."""
    google_config = await _async_setup(hass)
    hass.states.async_set("switch.outlet", "on", {"friendly_name": "Outlet"})

    with (
        patch(
            "homeassistant.components.google_assistant.http.async_integration_yaml_config",
            return_value=_yaml_config(
                entity_config={"switch.outlet": {"name": "Coffee machine"}}
            ),
        ),
        patch.object(GoogleConfig, "async_sync_entities_all") as mock_sync,
    ):
        result = await sh.async_handle_message(
            hass,
            google_config,
            "test-agent",
            "test-agent",
            {"requestId": "abc", "inputs": [{"intent": "action.devices.SYNC"}]},
            ga.const.SOURCE_CLOUD,
        )

    assert google_config.entity_config == {
        "switch.outlet": {"name": "Coffee machine", "expose": True}
    }
    devices = result["payload"]["devices"]
    assert [device["name"]["name"] for device in devices] == ["Coffee machine"]
    mock_sync.assert_not_called()


@pytest.mark.parametrize(
    "error", [HomeAssistantError("Invalid YAML"), FileNotFoundError("configuration")]
)
async def test_sync_request_unreadable_yaml(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """Test a SYNC request is answered when the YAML can't be read."""
    google_config = await _async_setup(hass)
    hass.states.async_set("switch.outlet", "on", {"friendly_name": "Outlet"})

    with patch(
        "homeassistant.components.google_assistant.http.async_integration_yaml_config",
        side_effect=error,
    ):
        result = await sh.async_handle_message(
            hass,
            google_config,
            "test-agent",
            "test-agent",
            {"requestId": "abc", "inputs": [{"intent": "action.devices.SYNC"}]},
            ga.const.SOURCE_CLOUD,
        )

    devices = result["payload"]["devices"]
    assert [device["name"]["name"] for device in devices] == ["Outlet"]
    assert "Could not reload the YAML configuration" in caplog.text
