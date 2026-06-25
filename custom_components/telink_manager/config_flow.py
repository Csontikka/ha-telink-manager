"""Config flow for Telink Manager.

The integration is panel-only and needs no user configuration: a single config entry simply
enables the sidebar panel. The flow therefore shows one confirmation step with no input fields.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN, PANEL_TITLE


class TelinkManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance, input-less config flow that enables the Telink Manager panel."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show a one-time confirmation, then create the single config entry."""
        # ponytail: single_config_entry (manifest) aborts a 2nd add with single_instance_allowed
        # before this step runs, so no in-flow duplicate guard is needed here.
        if user_input is not None:
            return self.async_create_entry(title=PANEL_TITLE, data={})
        return self.async_show_form(step_id="user")
