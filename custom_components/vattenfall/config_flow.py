"""Config flow for the Vattenfall supplier (Mina sidor) integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .api import Premise, VattenfallApiClient, VattenfallApiError, VattenfallAuthError
from .const import (
    CONF_CUSTOMER_ID,
    CONF_PASSWORD,
    CONF_PREMISE_ID,
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class VattenfallConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Vattenfall config flow."""

    VERSION = 2

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}
        self._premises: list[Premise] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: ask for credentials, authenticate and discover premises."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = VattenfallApiClient(
                hass=self.hass,
                customer_id=user_input[CONF_CUSTOMER_ID],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await client.async_authenticate(force=True)
                premises = await client.async_list_premises()
            except VattenfallAuthError:
                errors["base"] = "invalid_auth"
            except VattenfallApiError as err:
                _LOGGER.warning("Vattenfall API error during config flow: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during Vattenfall config flow")
                errors["base"] = "unknown"
            finally:
                await client.async_close()

            if not errors:
                if not premises:
                    errors["base"] = "no_premises"
                else:
                    self._pending = {
                        CONF_NAME: user_input.get(CONF_NAME, DEFAULT_NAME),
                        CONF_CUSTOMER_ID: user_input[CONF_CUSTOMER_ID],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    }
                    self._premises = premises
                    if len(premises) == 1:
                        return await self._async_finish(premises[0].premise_id)
                    return await self.async_step_premise()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CUSTOMER_ID): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_premise(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: let the user pick a premise when more than one is available."""
        if user_input is not None:
            return await self._async_finish(user_input[CONF_PREMISE_ID])

        options = {
            p.premise_id: (f"{p.premise_id} — {p.address}" if p.address else p.premise_id)
            for p in self._premises
        }
        schema = vol.Schema({vol.Required(CONF_PREMISE_ID): vol.In(options)})
        return self.async_show_form(step_id="premise", data_schema=schema)

    async def _async_finish(self, premise_id: str) -> FlowResult:
        await self.async_set_unique_id(premise_id)
        self._abort_if_unique_id_configured()

        data = {
            CONF_NAME: self._pending.get(CONF_NAME, DEFAULT_NAME),
            CONF_CUSTOMER_ID: self._pending[CONF_CUSTOMER_ID],
            CONF_PASSWORD: self._pending[CONF_PASSWORD],
            CONF_PREMISE_ID: premise_id,
        }
        return self.async_create_entry(title=data[CONF_NAME], data=data)
