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

from datetime import timedelta
import re
import secrets
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
    CONF_ENERGY,
    CONF_EXPIRES_AT,
    CONF_FRAME_ANCESTORS,
    CONF_FRIGATE_CAMERAS,
    CONF_FRIGATE_UI,
    CONF_FRIGATE_UI_PASSWORD,
    CONF_FRIGATE_UI_URL,
    CONF_FRIGATE_UI_USER,
    CONF_HIDE_LOCATION,
    CONF_LOADING_BACKGROUND,
    CONF_MORE,
    CONF_NAME,
    CONF_NO_EXPIRY,
    CONF_NOINDEX,
    CONF_PUBLIC_PATH,
    CONF_SHOW_TABS,
    CONF_SUPPORT_VIEWS,
    CONF_TITLE,
    CONF_VALID_FOR,
    CONF_VIEW_PATH,
    CONF_VIEWS,
    DEFAULT_NOINDEX,
    DEFAULT_PUBLIC_PATH,
    DOMAIN,
    RESERVED_PATHS,
)

# Frigate's own interface for visitors: offered once frigate_ui.py is in place.
FRIGATE_UI_READY = True

PATH_PATTERN = re.compile(r"^[a-z0-9_]{2,48}$")
COLOR_PATTERN = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")

from .sanitize import FIRST_VIEW, configured_views, sub_path  # noqa: E402


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


def validate_public_path(hass: Any, path: str, entry_id: str | None = None) -> str | None:
    """Return an error key, or None when the path is safe to claim.

    A link also answers at its address followed by an underscore and a name
    (its other panels: "/home_energy"), so neither a panel of Home Assistant
    nor another link may sit there, or the other way round."""
    if not PATH_PATTERN.match(path or ""):
        return "invalid_path"
    if path in RESERVED_PATHS:
        return "path_reserved"

    def clash(other: str) -> bool:
        return other == path or other.startswith(path + "_") or path.startswith(other + "_")

    if any(other == path or other.startswith(path + "_") for other in ha_data.registered_panel_paths(hass)):
        return "path_in_use"
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id:
            continue
        other = {**entry.data, **entry.options}.get(CONF_PUBLIC_PATH)
        if other and clash(str(other)):
            return "path_taken"
    return None


def random_path() -> str:
    """An address nobody can guess, for a link handed to one person."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def entry_title(name: str | None, path: str) -> str:
    return f"{name.strip()} (/{path})" if name and name.strip() else f"/{path}"


def expiry_from(hass: Any, user_input: dict[str, Any]) -> tuple[str | None, str | None]:
    """(when the link ends, error). "Valid for" counts from now and wins over
    a date; neither means the link does not end by itself."""
    from homeassistant.util import dt as dt_util

    valid_for = user_input.get(CONF_VALID_FOR)
    if isinstance(valid_for, dict):
        span = timedelta(
            days=valid_for.get("days", 0) or 0, hours=valid_for.get("hours", 0) or 0,
            minutes=valid_for.get("minutes", 0) or 0, seconds=valid_for.get("seconds", 0) or 0,
        )
        if span.total_seconds() > 0:
            return (dt_util.utcnow() + span).isoformat(timespec="seconds"), None
    raw = user_input.get(CONF_EXPIRES_AT)
    if not raw:
        return None, None
    parsed = dt_util.parse_datetime(str(raw))
    if parsed is None:
        return None, "invalid_expiry"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    if dt_util.as_utc(parsed) <= dt_util.utcnow():
        return None, "expiry_past"
    return dt_util.as_utc(parsed).isoformat(timespec="seconds"), None


def local_expiry(stored: Any) -> str | None:
    """A stored end time as the date-and-time field shows it."""
    from homeassistant.util import dt as dt_util

    parsed = dt_util.parse_datetime(str(stored)) if stored else None
    if parsed is None:
        return None
    return dt_util.as_local(parsed).strftime("%Y-%m-%d %H:%M:%S")


def life_fields(current: dict[str, Any] | None = None) -> dict[Any, Any]:
    """How long the link lasts: a span from now, or a date and time. A link
    that has a time limit also gets a way to take it away."""
    current = current or {}
    fields: dict[Any, Any] = {
        vol.Optional(CONF_VALID_FOR): selector.DurationSelector(
            selector.DurationSelectorConfig(enable_day=True)
        ),
        vol.Optional(
            CONF_EXPIRES_AT,
            description={"suggested_value": local_expiry(current.get(CONF_EXPIRES_AT))},
        ): selector.DateTimeSelector(),
    }
    if current.get(CONF_EXPIRES_AT):
        fields[vol.Required(CONF_NO_EXPIRY, default=False)] = bool
    return fields


def life_now(stored: Any) -> str:
    """The link's time limit as it stands, for the top of the settings form."""
    from homeassistant.util import dt as dt_util

    parsed = dt_util.parse_datetime(str(stored)) if stored else None
    if parsed is None:
        return "—"
    when = dt_util.as_local(parsed).strftime("%Y-%m-%d %H:%M")
    return f"{when} — EXPIRED" if dt_util.as_utc(parsed) <= dt_util.utcnow() else when


def more_fields(
    hass: Any, dashboards: list[dict[str, Any]], main: str | None, current: dict[str, Any] | None = None
) -> dict[Any, Any]:
    """What else the link shows besides its main dashboard."""
    current = current or {}
    chosen = [m["dashboard"] for m in current.get(CONF_MORE) or [] if isinstance(m, dict) and m.get("dashboard")]
    others = [
        selector.SelectOptionDict(value=d["url_path"], label=f"{d['title']} ({d['url_path']})")
        for d in dashboards
        if d["url_path"] != main
    ]
    known = {o["value"] for o in others}
    others += [selector.SelectOptionDict(value=c, label=f"{c} (not found)") for c in chosen if c not in known]
    fields: dict[Any, Any] = {}
    if others:
        fields[vol.Optional(CONF_MORE, default=[c for c in chosen if c != main])] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=others, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
    if "energy" in ha_data.registered_panel_paths(hass) or current.get(CONF_ENERGY):
        fields[vol.Required(CONF_ENERGY, default=bool(current.get(CONF_ENERGY)))] = bool
    if FRIGATE_UI_READY and (ha_data.frigate_instances(hass) or current.get(CONF_FRIGATE_UI)):
        from . import frigate_ui

        fields[vol.Required(CONF_FRIGATE_UI, default=bool(current.get(CONF_FRIGATE_UI)))] = bool
        fields[
            vol.Optional(
                CONF_FRIGATE_UI_URL,
                description={"suggested_value": current.get(CONF_FRIGATE_UI_URL) or frigate_ui.default_url(hass)},
            )
        ] = str
        fields[
            vol.Optional(CONF_FRIGATE_UI_USER, description={"suggested_value": current.get(CONF_FRIGATE_UI_USER) or ""})
        ] = str
        fields[
            vol.Optional(
                CONF_FRIGATE_UI_PASSWORD,
                description={"suggested_value": current.get(CONF_FRIGATE_UI_PASSWORD) or ""},
            )
        ] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
    return fields


async def frigate_ui_answers(
    hass: Any, user_input: dict[str, Any], published: set[str] | None = None
) -> tuple[dict[str, Any], str | None]:
    """The Frigate interface settings as stored, and an error key if the
    account cannot be used. Checked only when the interface is switched on.

    ``published`` are the Frigate cameras the link publishes, when that is
    known: an account that can see more is refused here, with a notification
    naming the cameras, rather than left to show an empty panel."""
    stored = {
        CONF_FRIGATE_UI: bool(user_input.get(CONF_FRIGATE_UI)),
        CONF_FRIGATE_UI_URL: (user_input.get(CONF_FRIGATE_UI_URL) or "").strip().rstrip("/"),
        CONF_FRIGATE_UI_USER: (user_input.get(CONF_FRIGATE_UI_USER) or "").strip(),
        CONF_FRIGATE_UI_PASSWORD: user_input.get(CONF_FRIGATE_UI_PASSWORD) or "",
    }
    if not stored[CONF_FRIGATE_UI]:
        return stored, None
    if not (stored[CONF_FRIGATE_UI_URL] and stored[CONF_FRIGATE_UI_USER] and stored[CONF_FRIGATE_UI_PASSWORD]):
        return stored, "frigate_missing"
    from . import frigate_ui

    error, cameras = await frigate_ui.async_check(
        hass, stored[CONF_FRIGATE_UI_URL], stored[CONF_FRIGATE_UI_USER], stored[CONF_FRIGATE_UI_PASSWORD]
    )
    if error is None and published is not None:
        beyond = sorted(set(cameras) - published)
        if beyond:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                hass,
                f"The Frigate account **{stored[CONF_FRIGATE_UI_USER]}** can see cameras the link does not"
                f" publish: **{', '.join(beyond)}**. Either tick them under \"Frigate cameras visitors may"
                " browse\", or give the account a role in Frigate (Settings → Users → Roles) that is"
                " limited to the cameras to show.",
                title="Public Access: Frigate account sees too much",
                notification_id="public_access_frigate_check",
            )
            return stored, "frigate_cameras_beyond"
    return stored, error


def stored_more(
    dashboards: list[dict[str, Any]], wanted: list[str], views: dict[str, list[str]],
    public_path: str, *, energy: bool, frigate: bool,
) -> list[dict[str, Any]]:
    """The link's other dashboards as stored: each with its views, its title
    and its public address (the link's, an underscore, the title)."""
    taken = {public_path}
    if energy:
        taken.add(f"{public_path}_energy")
    if frigate:
        taken.add(f"{public_path}_frigate")
    out = []
    for url_path in wanted:
        board = next((d for d in dashboards if d["url_path"] == url_path), None)
        title = (board or {}).get("title") or url_path
        path = sub_path(public_path, title, taken)
        taken.add(path)
        out.append({"dashboard": url_path, "title": title, "views": views.get(url_path) or [], "path": path})
    return out


class _MoreViews:
    """The step both flows share: for each further dashboard with several
    views, which of them. Everything is ticked to begin with; a view added to
    the dashboard later is not published until it is ticked here."""

    _dashboards: list[dict[str, Any]]
    _wanted: list[str]
    _pending: list[str]
    _views: dict[str, list[str]]

    def _start_more(self, wanted: list[str], before: dict[str, list[str]]) -> None:
        self._wanted = list(wanted)
        self._views = {}
        self._before = before
        self._pending = []
        for url_path in self._wanted:
            options = view_options(self._board(url_path))
            if len(options) <= 1:
                self._views[url_path] = [options[0]["value"]] if options else [FIRST_VIEW]
            else:
                self._pending.append(url_path)

    def _board(self, url_path: str) -> dict[str, Any]:
        return next((d for d in self._dashboards if d["url_path"] == url_path), {"views": []})

    async def async_step_more_views(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._pending:
            chosen = _stored_views(user_input.get(CONF_VIEWS))
            if chosen:
                self._views[self._pending.pop(0)] = chosen
            else:
                errors[CONF_VIEWS] = "no_views"
        if not self._pending:
            return await self._more_done()
        url_path = self._pending[0]
        board = self._board(url_path)
        options = view_options(board)
        before = [v for v in self._before.get(url_path) or [] if v in {o["value"] for o in options}]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="more_views",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VIEWS, default=before or [o["value"] for o in options]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, multiple=True, mode=selector.SelectSelectorMode.LIST
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"dashboard": board.get("title") or url_path},
        )


class PublicAccessConfigFlow(_MoreViews, ConfigFlow, domain=DOMAIN):
    """Make one link: who it is for, what it shows, its address, how long it lasts.

    Every entry of this integration is one link. Add another entry for
    another link, with its own selection and its own life."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._dashboards: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start with the dashboard: there is nothing else to ask first."""
        return await self.async_step_dashboard()

    async def async_step_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Who the link is for, and its main dashboard."""
        self._dashboards = await ha_data.async_list_publishable_dashboards(self.hass)
        if not self._dashboards:
            return self.async_abort(reason="no_dashboards")

        if user_input is not None:
            self._data[CONF_DASHBOARD] = user_input[CONF_DASHBOARD]
            self._options[CONF_NAME] = (user_input.get(CONF_NAME) or "").strip()
            views = view_options(self._dashboard(user_input[CONF_DASHBOARD]))
            if len(views) <= 1:
                # One view (or none with an address): nothing to choose.
                self._data[CONF_VIEWS] = [views[0]["value"]] if views else [FIRST_VIEW]
                return await self.async_step_more()
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
                    vol.Optional(CONF_NAME): str,
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
                return await self.async_step_more()
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

    async def async_step_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What else the link shows: further dashboards, Energy, Frigate."""
        fields = more_fields(self.hass, self._dashboards, self._data[CONF_DASHBOARD])
        if not fields:
            self._start_more([], {})
            return await self._more_done()
        errors: dict[str, str] = {}
        if user_input is not None:
            frigate, error = await frigate_ui_answers(self.hass, user_input)
            if error:
                errors[CONF_FRIGATE_UI] = error
            else:
                self._options[CONF_ENERGY] = bool(user_input.get(CONF_ENERGY))
                self._options.update(frigate)
                self._start_more(_stored_views(user_input.get(CONF_MORE) or []), {})
                return await self.async_step_more_views()
        return self.async_show_form(step_id="more", data_schema=vol.Schema(fields), errors=errors)

    async def _more_done(self) -> ConfigFlowResult:
        return await self.async_step_path()

    async def async_step_path(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The link's address, and how long it lasts."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input[CONF_PUBLIC_PATH].strip().strip("/").lower()
            error = validate_public_path(self.hass, path)
            expires, expiry_error = expiry_from(self.hass, user_input)
            if error:
                errors[CONF_PUBLIC_PATH] = error
            elif expiry_error:
                errors[CONF_EXPIRES_AT] = expiry_error
            else:
                self._data[CONF_PUBLIC_PATH] = path
                return self.async_create_entry(
                    title=entry_title(self._options.get(CONF_NAME), path),
                    data=self._data,
                    options={
                        CONF_ENABLED: True,
                        CONF_NOINDEX: DEFAULT_NOINDEX,
                        CONF_SHOW_TABS: True,
                        CONF_CAMERA_STREAMS: True,
                        CONF_NAME: self._options.get(CONF_NAME) or "",
                        CONF_ENERGY: bool(self._options.get(CONF_ENERGY)),
                        CONF_FRIGATE_UI: bool(self._options.get(CONF_FRIGATE_UI)),
                        CONF_FRIGATE_UI_URL: self._options.get(CONF_FRIGATE_UI_URL) or "",
                        CONF_FRIGATE_UI_USER: self._options.get(CONF_FRIGATE_UI_USER) or "",
                        CONF_FRIGATE_UI_PASSWORD: self._options.get(CONF_FRIGATE_UI_PASSWORD) or "",
                        CONF_MORE: stored_more(
                            self._dashboards, self._wanted, self._views, path,
                            energy=bool(self._options.get(CONF_ENERGY)),
                            frigate=bool(self._options.get(CONF_FRIGATE_UI)),
                        ),
                        CONF_EXPIRES_AT: expires or "",
                    },
                )

        # The first link keeps the plain address; every further one starts
        # with an address nobody can guess.
        first = not self._async_current_entries()
        suggested = (user_input or {}).get(CONF_PUBLIC_PATH) or (DEFAULT_PUBLIC_PATH if first else random_path())
        return self.async_show_form(
            step_id="path",
            data_schema=vol.Schema(
                {vol.Required(CONF_PUBLIC_PATH, default=suggested): str, **life_fields()}
            ),
            errors=errors,
            description_placeholders={"base_url": public_base_url(self.hass)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PublicAccessOptionsFlow()


class PublicAccessOptionsFlow(_MoreViews, OptionsFlow):
    """Change what a link shows, where, and for how long."""

    _saved: dict[str, Any]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            path = user_input[CONF_PUBLIC_PATH].strip().strip("/").lower()
            if path != current.get(CONF_PUBLIC_PATH):
                error = validate_public_path(self.hass, path, self.config_entry.entry_id)
                if error:
                    errors[CONF_PUBLIC_PATH] = error
            expires, expiry_error = expiry_from(self.hass, user_input)
            if expiry_error == "expiry_past" and local_expiry(current.get(CONF_EXPIRES_AT)) == str(
                user_input.get(CONF_EXPIRES_AT)
            ):
                # An expired link's date, left as it was: still expired.
                expires, expiry_error = str(current.get(CONF_EXPIRES_AT)), None
            if user_input.get(CONF_NO_EXPIRY):
                # Ticked: no time limit, whatever the two fields above say.
                expires, expiry_error = None, None
            if expiry_error:
                errors[CONF_EXPIRES_AT] = expiry_error
            published = set(_stored_views(user_input.get(CONF_FRIGATE_CAMERAS)))
            try:
                access = await self.hass.data[DOMAIN][self.config_entry.entry_id]["coordinator"].async_access()
                published |= set(access.frigate.cameras) if access.frigate else set()
            except Exception:  # noqa: BLE001 - the entry is not loaded: the ticks alone decide
                pass
            frigate, frigate_error = await frigate_ui_answers(self.hass, user_input, published)
            if frigate_error:
                errors[CONF_FRIGATE_UI] = frigate_error
            color = (user_input.get(CONF_LOADING_BACKGROUND) or "").strip().lower()
            if color and not COLOR_PATTERN.match(color):
                errors[CONF_LOADING_BACKGROUND] = "invalid_color"
            chosen = _stored_views(user_input.get(CONF_VIEWS))
            if not chosen:
                errors[CONF_VIEWS] = "no_views"
            support = [v for v in _stored_views(user_input.get(CONF_SUPPORT_VIEWS)) if v not in chosen]
            if not errors:
                self._saved = {
                    **{k: v for k, v in user_input.items() if k not in (CONF_VALID_FOR, CONF_NO_EXPIRY)},
                    CONF_LOADING_BACKGROUND: color,
                    CONF_PUBLIC_PATH: path,
                    CONF_VIEWS: chosen,
                    CONF_SUPPORT_VIEWS: support,
                    CONF_TITLE: (user_input.get(CONF_TITLE) or "").strip(),
                    CONF_NAME: (user_input.get(CONF_NAME) or "").strip(),
                    CONF_ENERGY: bool(user_input.get(CONF_ENERGY)),
                    **frigate,
                    CONF_EXPIRES_AT: expires or "",
                    CONF_FRIGATE_CAMERAS: _stored_views(user_input.get(CONF_FRIGATE_CAMERAS)),
                    # What versions before 0.6 read: the first chosen view.
                    CONF_VIEW_PATH: _stored_view(chosen[0]),
                }
                self._dashboards = await ha_data.async_list_publishable_dashboards(self.hass)
                wanted = [d for d in _stored_views(user_input.get(CONF_MORE) or []) if d != user_input[CONF_DASHBOARD]]
                before = {
                    m["dashboard"]: list(m.get("views") or [])
                    for m in current.get(CONF_MORE) or [] if isinstance(m, dict) and m.get("dashboard")
                }
                self._start_more(wanted, before)
                return await self.async_step_more_views()

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
            vol.Optional(CONF_NAME, description={"suggested_value": current.get(CONF_NAME) or ""}): str,
            **life_fields(current),
            vol.Required(
                CONF_DASHBOARD, default=current.get(CONF_DASHBOARD)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_VIEWS, default=chosen_views): view_selector(chosen_views),
            vol.Optional(CONF_SUPPORT_VIEWS, default=support_views): view_selector(support_views),
            **more_fields(self.hass, dashboards, current.get(CONF_DASHBOARD), current),
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
                CONF_HIDE_LOCATION, default=current.get(CONF_HIDE_LOCATION, True)
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
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"life": life_now(current.get(CONF_EXPIRES_AT))},
        )

    async def _more_done(self) -> ConfigFlowResult:
        saved = self._saved
        path = saved[CONF_PUBLIC_PATH]
        saved[CONF_MORE] = stored_more(
            self._dashboards, self._wanted, self._views, path,
            energy=saved[CONF_ENERGY], frigate=saved[CONF_FRIGATE_UI],
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry, title=entry_title(saved.get(CONF_NAME), path)
        )
        return self.async_create_entry(data=saved)
