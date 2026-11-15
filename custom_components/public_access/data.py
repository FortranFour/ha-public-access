"""The only module that touches Home Assistant internals.

Every call here is a read. There is deliberately no code path in this package that
calls a service, writes a config, or mutates state — a test asserts that.

The API shapes used here were verified against Home Assistant 2026.9.3.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)

def registered_panel_paths(hass: HomeAssistant) -> set[str]:
    """Every path the frontend already owns, including storage dashboards.

    A registered aiohttp view outranks the frontend's catch-all resource, so a
    public path colliding with one of these would shadow the owner's own UI.
    """
    try:
        from homeassistant.components import frontend

        return set(hass.data.get(frontend.DATA_PANELS, {}) or {})
    except Exception:  # noqa: BLE001 - never block setup on introspection
        _LOGGER.debug("Could not read the panel list", exc_info=True)
        return set()


def _dashboards(hass: HomeAssistant) -> dict[str | None, Any]:
    from homeassistant.components import lovelace

    data = hass.data.get(lovelace.DOMAIN)
    return getattr(data, "dashboards", None) or {}


async def async_list_publishable_dashboards(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Dashboards that actually have a stored config.

    An auto-generated dashboard raises ConfigNotFound, so it cannot be published;
    the config flow offers only what is listed here.
    """
    out: list[dict[str, Any]] = []
    for url_path, store in _dashboards(hass).items():
        if url_path is None:
            # The default dashboard has no stable public URL of its own and is
            # usually strategy-generated; skip it rather than confuse the user.
            continue
        try:
            config = await store.async_load(False)
        except Exception:  # noqa: BLE001 - ConfigNotFound and friends
            continue
        if not isinstance(config, dict):
            continue
        views = [v for v in config.get("views", []) if isinstance(v, dict)]
        out.append(
            {
                "url_path": url_path,
                "title": config.get("title") or url_path,
                "views": [
                    {"path": v.get("path"), "title": v.get("title")} for v in views
                ],
            }
        )
    return sorted(out, key=lambda item: item["url_path"])


async def async_load_dashboard_config(
    hass: HomeAssistant, url_path: str
) -> dict[str, Any] | None:
    """Load one dashboard's stored config, or None when it has none."""
    store = _dashboards(hass).get(url_path)
    if store is None:
        return None
    try:
        config = await store.async_load(False)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Dashboard %s has no stored config", url_path, exc_info=True)
        return None
    return config if isinstance(config, dict) else None


async def async_energy_prefs(hass: HomeAssistant) -> dict[str, Any] | None:
    """The energy preferences, read through the energy manager."""
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        return manager.data
    except Exception:  # noqa: BLE001 - energy may not be set up at all
        _LOGGER.debug("Energy preferences unavailable", exc_info=True)
        return None


def energy_statistic_ids(prefs: dict[str, Any] | None) -> set[str]:
    """Statistic ids referenced by the energy preferences.

    Handles both the unified grid schema (flat stat_energy_from/stat_energy_to)
    and the legacy flow_from/flow_to shape, since stored preferences on an
    existing install may not have been migrated yet.
    """
    ids: set[str] = set()
    if not prefs:
        return ids

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            ids.add(value)

    for source in prefs.get("energy_sources") or []:
        if not isinstance(source, dict):
            continue
        for key in (
            "stat_energy_from",
            "stat_energy_to",
            "stat_cost",
            "stat_compensation",
            "stat_rate",
        ):
            add(source.get(key))
        # Legacy grid shape.
        for flow in (source.get("flow_from") or []) + (source.get("flow_to") or []):
            if isinstance(flow, dict):
                for key in (
                    "stat_energy_from",
                    "stat_energy_to",
                    "stat_cost",
                    "stat_compensation",
                ):
                    add(flow.get(key))
    for device in prefs.get("device_consumption") or []:
        if isinstance(device, dict):
            add(device.get("stat_consumption"))
    for device in prefs.get("device_consumption_water") or []:
        if isinstance(device, dict):
            add(device.get("stat_consumption"))
    return ids



def device_ids(hass: HomeAssistant, entity_ids: set[str]) -> set[str]:
    """The devices behind these entities, from the entity registry."""
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        found: set[str] = set()
        for entity_id in entity_ids:
            entry = registry.async_get(entity_id)
            if entry is not None and entry.device_id:
                found.add(entry.device_id)
        return found
    except Exception:  # noqa: BLE001 - never block a visit on introspection
        _LOGGER.debug("Could not read the entity registry", exc_info=True)
        return set()


def frigate_instances(hass: HomeAssistant) -> dict[str, set[str]]:
    """Every Frigate instance the Frigate integration knows, with its cameras.

    Keyed by the instance id cards use (Frigate's MQTT client id). Read from
    the Frigate integration's own data; empty when it is not installed.
    """
    out: dict[str, set[str]] = {}
    try:
        for stored in (hass.data.get("frigate") or {}).values():
            config = stored.get("config") if isinstance(stored, dict) else None
            if not isinstance(config, dict):
                continue
            instance = (config.get("mqtt") or {}).get("client_id") or "frigate"
            cameras = config.get("cameras") or {}
            out.setdefault(str(instance), set()).update(str(name) for name in cameras)
    except Exception:  # noqa: BLE001 - another integration's data: assume nothing
        _LOGGER.debug("Could not read the Frigate integration's data", exc_info=True)
        return {}
    return out


def frigate_cameras_of(hass: HomeAssistant, entity_ids: set[str]) -> set[str]:
    """Frigate camera names behind these camera entities.

    A Frigate camera entity says which camera it is in its ``camera_name``
    attribute.
    """
    names: set[str] = set()
    for entity_id in entity_ids:
        if not entity_id.startswith("camera."):
            continue
        state = hass.states.get(entity_id)
        name = state.attributes.get("camera_name") if state is not None else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


async def async_fetch_own_json(hass: HomeAssistant, signed_path: str) -> Any | None:
    """Read JSON from this Home Assistant's own HTTP interface.

    Used for the one answer that has to be cleaned before a visitor may have
    it (a live stream's description): the Frigate integration serves it over
    HTTP only, so it is fetched from here, over the loopback address, with a
    link signed for the read-only viewer. Returns None when it cannot be read.
    """
    try:
        from aiohttp import ClientTimeout

        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        api = hass.config.api
        if api is None:
            return None
        scheme = "https" if api.use_ssl else "http"
        # A certificate is issued for the public name, not for 127.0.0.1.
        session = async_get_clientsession(hass, verify_ssl=False)
        for host in ("127.0.0.1", api.local_ip):
            try:
                url = f"{scheme}://{host}:{api.port}{signed_path}"
                async with session.get(url, timeout=ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)
                    _LOGGER.debug("Own request answered %s", response.status)
                    return None
            except OSError:
                continue  # not listening on this address: try the next
    except Exception:  # noqa: BLE001 - a missing description is not worth a failed page
        _LOGGER.debug("Could not read from Home Assistant's own interface", exc_info=True)
    return None


async def async_uix_foundries(hass: HomeAssistant) -> dict[str, Any] | None:
    """UIX's foundries (shared card definitions), as UIX hands them to its
    frontend; None when UIX is not installed.

    Read through UIX's own helper when it is loaded, which also reads its
    foundry files; otherwise the ones stored in its options.
    """
    try:
        entries = hass.config_entries.async_entries("uix")
        if not entries:
            return None
        options = entries[0].options
        stored = dict(options.get("foundries") or {})
        files = list(options.get("foundry_files") or [])
        import sys

        helpers = sys.modules.get("custom_components.uix.helpers")
        reader = getattr(helpers, "get_all_foundries", None)
        if reader is None:
            return stored
        result = await hass.async_add_executor_job(reader, hass, stored, files)
        return result if isinstance(result, dict) else stored
    except Exception:  # noqa: BLE001 - another integration's data: assume nothing
        _LOGGER.debug("Could not read UIX's foundries", exc_info=True)
        return {}
