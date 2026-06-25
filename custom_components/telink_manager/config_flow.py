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
        """Handle the user-initiated step (a simple confirmation)."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title=PANEL_TITLE, data={})
        return self.async_show_form(step_id="user")
