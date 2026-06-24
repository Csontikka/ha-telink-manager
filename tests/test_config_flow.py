"""Tests for the Telink Manager config flow."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.telink_manager.const import DOMAIN, PANEL_TITLE


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The user step shows a form, then creates a single entry on confirm."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch("custom_components.telink_manager.async_setup_entry", return_value=True) as mock_setup:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PANEL_TITLE
    assert result["data"] == {}
    assert len(mock_setup.mock_calls) == 1


async def test_single_instance_aborts(hass: HomeAssistant) -> None:
    """A second user flow aborts because only one instance is allowed."""
    with patch("custom_components.telink_manager.async_setup_entry", return_value=True):
        first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        await hass.config_entries.flow.async_configure(first["flow_id"], {})
        await hass.async_block_till_done()

    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "single_instance_allowed"


async def test_import_flow_creates_entry(hass: HomeAssistant) -> None:
    """The import step (legacy YAML migration) creates an entry directly."""
    with patch("custom_components.telink_manager.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data={}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PANEL_TITLE
