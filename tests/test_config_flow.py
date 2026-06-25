"""Tests for the Telink Manager config flow."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telink_manager.const import DOMAIN, PANEL_TITLE


@pytest.fixture(autouse=True)
def _deps_loaded(hass: HomeAssistant) -> None:
    # ponytail: the flow needs none of the manifest deps; mark them loaded so starting the flow
    # / creating the entry doesn't try to set up panel_custom/frontend in the bare test env.
    for comp in ("http", "websocket_api", "frontend", "panel_custom"):
        hass.config.components.add(comp)


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """The first call shows the no-input confirmation form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_step_creates_entry(hass: HomeAssistant) -> None:
    """Confirming creates the single config entry."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.telink_manager.async_setup_entry", return_value=True) as mock_setup:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PANEL_TITLE
    assert result["data"] == {}
    assert len(mock_setup.mock_calls) == 1


async def test_single_instance_only(hass: HomeAssistant) -> None:
    """A second add aborts (single_config_entry in the manifest)."""
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
