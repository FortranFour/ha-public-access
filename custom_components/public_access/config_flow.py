"""Config and options flow.

The public path check here is a safety control, not cosmetics. A registered aiohttp
view outranks the frontend's catch-all resource, so a path that collides with a
panel or a dashboard would shadow part of the owner's own Home Assistant. Two rules
keep that impossible:

* the path must contain no hyphen — Home Assistant requires every storage dashboard
  `url_path` to contain one, so a hyphen-free path can never collide with a dashboard;
* the path must not be a reserved path or a currently registered panel.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import data as ha_data
from .const import (
    CONF_CAMERA_STREAMS,
    CONF_DASHBOARD,
    CONF_ENABLED,
    CONF_FRAME_ANCESTORS,
    CONF_FRIGATE_CAMERAS,
    CONF_LOADING_BACKGROUND,
    CONF_NOINDEX,
    CONF_PUBLIC_PATH,
    CONF_SHOW_TABS,
    CONF_SUPPORT_VIEWS,
    CONF_TITLE,
    CONF_VIEW_PATH,
    CONF_VIEWS,
    DEFAULT_NOINDEX,
    DEFAULT_PUBLIC_PATH,
    DOMAIN,
    RESERVED_PATHS,
)

PATH_PATTERN = re.compile(r"^[a-z0-9_]{2,48}$")
COLOR_PATTERN = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")

from .sanitize import FIRST_VIEW, configured_views  # noqa: E402


def view_options(dashboard: dict[str, Any]) -> list[selector.SelectOptionDict]:
    """The dashboard's views as dropdown choices, by name.

    A view is found by its address (the last part of its URL). A view without
    one can only be reached as the first view; any other view without an
    address cannot be picked until it is given one in its settings.
    """
    options: list[selector.SelectOptionDict] = []
    for index, view in enumerate(dashboard.get("views") or []):
        path = view.get("path")
        title = view.get("title") or f"View {index + 1}"
        if path is None or str(path) == "":
            if index == 0:
                options.append(selector.SelectOptionDict(value=FIRST_VIEW, label=f"{title} — first view"))
            continue
        options.append(selector.SelectOptionDict(value=str(path), label=f"{title} — /{path}"))
    return options


def _stored_view(value: str | None) -> str | None:
    return None if not value or value == FIRST_VIEW else value


def _stored_views(values: Any) -> list[str]:
    """The chosen views as stored: names, without blanks or repeats, in order."""
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else [values]:
        name = str(value).strip().strip("/") if value is not None else ""
        if name and name not in out:
            out.append(name)
    return out


def _with_missing(
    views: list[selector.SelectOptionDict], chosen: list[str]
) -> list[selector.SelectOptionDict]:
    """Keep a chosen view in the list even when the dashboard no longer has it,
    so opening the options does not silently drop it."""
    known = {v["value"] for v in views}
    return views + [
        selector.SelectOptionDict(value=name, label=f"/{name} (not found)")
        for name in chosen
        if name not in known
    ]


def public_base_url(hass: Any) -> str:
    """This Home Assistant's address as the internet sees it, for the example
    shown in the setup form; a placeholder when none is configured."""
    try:
        from homeassistant.helpers.network import get_url

        return get_url(hass, prefer_external=True, allow_internal=True).rstrip("/")
    except Exception:  # noqa: BLE001 - NoURLAvailableError and friends
        return "https://your-home-assistant"


def validate_public_path(hass: Any, path: str) -> str | None:
    """Return an error key, or None when the path is safe to claim."""
    if not PATH_PATTERN.match(path or ""):
        return "invalid_path"
    if path in RESERVED_PATHS:
        return "path_reserved"
    if path in ha_data.registered_panel_paths(hass):
        return "path_in_use"
    return None


class PublicAccessConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the owner through dashboard, view and public path."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._dashboards: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start with the dashboard: there is nothing else to ask first."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_dashboard()

    async def async_step_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which dashboard, and which view of it, to publish."""
        self._dashboards = await ha_data.async_list_publishable_dashboards(self.hass)
        if not self._dashboards:
            return self.async_abort(reason="no_dashboards")

        if user_input is not None:
            self._data[CONF_DASHBOARD] = user_input[CONF_DASHBOARD]
            views = view_options(self._dashboard(user_input[CONF_DASHBOARD]))
            if len(views) <= 1:
                # One view (or none with an address): nothing to choose.
                self._data[CONF_VIEWS] = [views[0]["value"]] if views else [FIRST_VIEW]
                return await self.async_step_path()
            return await self.async_step_view()

        options = [
            selector.SelectOptionDict(
                value=item["url_path"], label=f"{item['title']} ({item['url_path']})"
            )
            for item in self._dashboards
        ]
        return self.async_show_form(
            step_id="dashboard",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DASHBOARD): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                }
            ),
        )

    def _dashboard(self, url_path: str) -> dict[str, Any]:
        return next((d for d in self._dashboards if d["url_path"] == url_path), {"views": []})

    async def async_step_view(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the views of the dashboard that will be public, by name."""
        dashboard = self._dashboard(self._data[CONF_DASHBOARD])
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = _stored_views(user_input.get(CONF_VIEWS))
            if chosen:
                self._data[CONF_VIEWS] = chosen
                return await self.async_step_path()
            errors[CONF_VIEWS] = "no_views"

        views = view_options(dashboard)
        return self.async_show_form(
            step_id="view",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VIEWS, default=[views[0]["value"]]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=views, multiple=True, mode=selector.SelectSelectorMode.LIST
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"dashboard": dashboard.get("title", "")},
        )

    async def async_step_path(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the public path, validated against panels and dashboards."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input[CONF_PUBLIC_PATH].strip().strip("/").lower()
            error = validate_public_path(self.hass, path)
            if error:
                errors[CONF_PUBLIC_PATH] = error
            else:
                self._data[CONF_PUBLIC_PATH] = path
                return self.async_create_entry(
                    title=f"/{path}",
                    data=self._data,
                    options={
                        CONF_ENABLED: True,
                        CONF_NOINDEX: DEFAULT_NOINDEX,
                        CONF_SHOW_TABS: True,
                        CONF_CAMERA_STREAMS: True,
                    },
                )

        return self.async_show_form(
            step_id="path",
            data_schema=vol.Schema(
                {vol.Required(CONF_PUBLIC_PATH, default=DEFAULT_PUBLIC_PATH): str}
            ),
            errors=errors,
            description_placeholders={"base_url": public_base_url(self.hass)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PublicAccessOptionsFlow()


class PublicAccessOptionsFlow(OptionsFlow):
    """Change what is published and how."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            path = user_input[CONF_PUBLIC_PATH].strip().strip("/").lower()
            if path != current.get(CONF_PUBLIC_PATH):
                error = validate_public_path(self.hass, path)
                if error:
                    errors[CONF_PUBLIC_PATH] = error
            color = (user_input.get(CONF_LOADING_BACKGROUND) or "").strip().lower()
            if color and not COLOR_PATTERN.match(color):
                errors[CONF_LOADING_BACKGROUND] = "invalid_color"
            chosen = _stored_views(user_input.get(CONF_VIEWS))
            if not chosen:
                errors[CONF_VIEWS] = "no_views"
            support = [v for v in _stored_views(user_input.get(CONF_SUPPORT_VIEWS)) if v not in chosen]
            if not errors:
                return self.async_create_entry(
                    data={
                        **user_input,
                        CONF_LOADING_BACKGROUND: color,
                        CONF_PUBLIC_PATH: path,
                        CONF_VIEWS: chosen,
                        CONF_SUPPORT_VIEWS: support,
                        CONF_TITLE: (user_input.get(CONF_TITLE) or "").strip(),
                        CONF_FRIGATE_CAMERAS: _stored_views(user_input.get(CONF_FRIGATE_CAMERAS)),
                        # What versions before 0.6 read: the first chosen view.
                        CONF_VIEW_PATH: _stored_view(chosen[0]),
                    }
                )

        dashboards = await ha_data.async_list_publishable_dashboards(self.hass)
        current_dashboard = next(
            (d for d in dashboards if d["url_path"] == current.get(CONF_DASHBOARD)), {"views": []}
        )
        views = view_options(current_dashboard)
        chosen_views = configured_views(current, CONF_VIEWS)
        if chosen_views == [FIRST_VIEW] and views and views[0]["value"] != FIRST_VIEW:
            # "The first view", from before views were chosen by name.
            chosen_views = [views[0]["value"]]
        support_views = configured_views(current, CONF_SUPPORT_VIEWS)
        options = [
            selector.SelectOptionDict(
                value=item["url_path"], label=f"{item['title']} ({item['url_path']})"
            )
            for item in dashboards
        ] or [
            selector.SelectOptionDict(
                value=current.get(CONF_DASHBOARD, ""), label=current.get(CONF_DASHBOARD, "")
            )
        ]

        def view_selector(chosen: list[str]) -> selector.SelectSelector:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_with_missing(views, chosen),
                    multiple=True,
                    custom_value=True,  # another dashboard's views can be typed
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        fields: dict[Any, Any] = {
            vol.Required(CONF_ENABLED, default=current.get(CONF_ENABLED, True)): bool,
            vol.Required(
                CONF_DASHBOARD, default=current.get(CONF_DASHBOARD)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_VIEWS, default=chosen_views): view_selector(chosen_views),
            vol.Optional(CONF_SUPPORT_VIEWS, default=support_views): view_selector(support_views),
            vol.Required(CONF_SHOW_TABS, default=current.get(CONF_SHOW_TABS, True)): bool,
            vol.Optional(
                CONF_TITLE, description={"suggested_value": current.get(CONF_TITLE) or ""}
            ): str,
            vol.Required(
                CONF_PUBLIC_PATH, default=current.get(CONF_PUBLIC_PATH)
            ): str,
            vol.Required(
                CONF_NOINDEX, default=current.get(CONF_NOINDEX, DEFAULT_NOINDEX)
            ): bool,
            vol.Required(
                CONF_CAMERA_STREAMS, default=current.get(CONF_CAMERA_STREAMS, True)
            ): bool,
        }
        # Offered only when the Frigate integration is there to ask.
        frigate = ha_data.frigate_instances(self.hass)
        chosen_cameras = [str(c) for c in current.get(CONF_FRIGATE_CAMERAS) or []]
        if frigate or chosen_cameras:
            cameras = sorted(set().union(*frigate.values()) | set(chosen_cameras)) if frigate else chosen_cameras
            fields[vol.Optional(CONF_FRIGATE_CAMERAS, default=chosen_cameras)] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=cameras,
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        fields[
            vol.Optional(
                CONF_FRAME_ANCESTORS,
                description={"suggested_value": current.get(CONF_FRAME_ANCESTORS) or ""},
            )
        ] = str
        fields[
            vol.Optional(
                CONF_LOADING_BACKGROUND,
                description={"suggested_value": current.get(CONF_LOADING_BACKGROUND) or ""},
            )
        ] = str
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields), errors=errors)
