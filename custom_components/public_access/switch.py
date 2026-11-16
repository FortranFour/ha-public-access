"""One switch per link. On: the link works. Off: it is revoked.

Switching it off answers "not found" at the link's address and hangs up on
everyone looking at it, at once; switching it on brings the same address
back. It is the same setting as "Link is active" in the link's options, here
so that it can sit on a dashboard or in an automation.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_ENABLED, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Any) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([PublicLinkSwitch(entry, coordinator)])


class PublicLinkSwitch(SwitchEntity):
    """The link, as something to switch off."""

    _attr_should_poll = False
    _attr_icon = "mdi:link-variant"

    def __init__(self, entry: ConfigEntry, coordinator: Any) -> None:
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_link"
        # Named after who the link is for, never after its address: a switch
        # can end up on a dashboard, and the address is the key.
        self._attr_name = f"Public link {coordinator.name}".strip()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{DOMAIN}_{self._entry.entry_id}_changed", self.async_write_ha_state
            )
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.options.get(CONF_ENABLED, True))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        expires = self._coordinator.expires_at
        return {
            "expires_at": expires.isoformat() if expires else None,
            "expired": self._coordinator.expired,
            "working": self._coordinator.live,
        }

    async def _set(self, enabled: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_ENABLED: enabled}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
