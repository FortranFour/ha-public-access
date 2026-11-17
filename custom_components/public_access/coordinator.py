"""What the mirror may read for one published dashboard.

Little is held between visits. What the published views allow is worked out
from the dashboard as it is saved, kept for half a minute so that a page full
of camera thumbnails does not read the dashboard once per picture, and dropped
at once when the dashboard or the options change. The other thing remembered
is which Frigate events were already listed to visitors (see
policy.EventMemory).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import autoentities, data as ha_data
from .const import (
    CONF_CAMERA_STREAMS,
    CONF_DASHBOARD,
    CONF_ENABLED,
    CONF_ENERGY,
    CONF_EXPIRES_AT,
    CONF_FRIGATE_CAMERAS,
    CONF_FRIGATE_UI,
    CONF_HIDE_LOCATION,
    CONF_MORE,
    CONF_NAME,
    CONF_PUBLIC_PATH,
    CONF_SHOW_TABS,
    CONF_SUPPORT_VIEWS,
    CONF_TITLE,
    CONF_VIEWS,
)
from .policy import (
    Access,
    EventMemory,
    FrigateAccess,
    MediaMemory,
    foundries_used,
    frigate_cameras_in,
)
from .sanitize import (
    ALL_VIEWS,
    Panel,
    collect_conditions,
    collect_strings,
    configured_views,
    has_published_view,
    publish_config,
    referenced_ids,
    sub_path,
)

_LOGGER = logging.getLogger(__name__)

# A card type that draws from the energy settings: Home Assistant's own
# energy-* and power-* cards, and custom flow and sankey cards.
_ENERGY_CARD = re.compile(r"^(?:custom:)?[\w-]*(?:energy|power-flow|power-sources|power-sankey|sankey)[\w-]*$")

# How long the worked-out access is reused. Short: an entity that appears in
# Home Assistant after a restart is picked up without anyone doing anything.
ACCESS_SECONDS = 30


class PublicDashboardCoordinator:
    """One published dashboard: its options, and what every visitor may read."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # Cleared on unload: the route cannot be unregistered, so the view checks
        # this instead and answers 404 once the entry is gone.
        self.active = True
        self._frigate_events = EventMemory()
        self._listed_media = MediaMemory()
        self._access: Access | None = None
        self._access_at = 0.0
        # What the dashboards and settings said the last time the access was
        # worked out (see _async_read_access); a change hangs up on visitors.
        self._said: tuple[Any, ...] | None = None
        # What visitors' pages asked for and did not get, for the owner's
        # diagnostics: the latest few, each kind of refusal once.
        self._refusals: deque[str] = deque(maxlen=40)
        # Every kind of message the mirror turned down, with how often, and
        # what visitors' pages reported going wrong in them.
        self._refused_types: dict[str, int] = {}
        self._page_errors: deque[str] = deque(maxlen=40)
        # Every socket open to a visitor of this link (the page's own, live
        # streams). Closed when the link is revoked, expires or changes.
        self.sockets: set[Any] = set()
        # The link's way into Frigate's own interface (frigate_ui.FrigateUi),
        # made on first use.
        self.frigate_ui: Any = None

    # -- configuration ---------------------------------------------------------

    @property
    def options(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def dashboard(self) -> str:
        return self.options.get(CONF_DASHBOARD) or ""

    @property
    def public_path(self) -> str:
        return self.options.get(CONF_PUBLIC_PATH) or ""

    @property
    def name(self) -> str:
        """Who the link is for, in the owner's words."""
        return (self.options.get(CONF_NAME) or "").strip()

    @property
    def expires_at(self) -> datetime | None:
        """When the link stops working by itself, or None: never."""
        from homeassistant.util import dt as dt_util

        raw = self.options.get(CONF_EXPIRES_AT)
        if not raw:
            return None
        parsed = dt_util.parse_datetime(str(raw))
        if parsed is None:
            return None
        return dt_util.as_utc(parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))

    @property
    def expired(self) -> bool:
        from homeassistant.util import dt as dt_util

        expires = self.expires_at
        return expires is not None and dt_util.utcnow() >= expires

    @property
    def live(self) -> bool:
        """Does the link work right now? Not once it is removed, switched
        off (revoked) or past its date."""
        return self.active and bool(self.options.get(CONF_ENABLED, True)) and not self.expired

    async def async_close_sockets(self) -> None:
        """Hang up on everyone using this link."""
        for socket in list(self.sockets):
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
        self.sockets.clear()
        if self.frigate_ui is not None and not self.active:
            await self.frigate_ui.async_close()
            self.frigate_ui = None

    @property
    def panels(self) -> list[Panel]:
        """Everything this link shows, the main dashboard first. Each panel
        has its own public address: the link's, or the link's with a suffix."""
        hass = getattr(self, "hass", None)

        def look(url_path: str) -> dict[str, Any]:
            return (ha_data.panel_look(hass, url_path) if hass is not None else None) or {}

        main = Panel(
            path=self.public_path,
            dashboard=self.dashboard,
            title=self.public_title or look(self.dashboard).get("title"),
            views=tuple(self.published_views),
            support=tuple(self.support_views),
            icon=look(self.dashboard).get("icon"),
        )
        out = [main]
        taken = {main.path}
        for item in self.options.get(CONF_MORE) or []:
            if not isinstance(item, dict) or not item.get("dashboard"):
                continue
            if item["dashboard"] in {p.dashboard for p in out}:
                continue
            path = str(item.get("path") or "")
            if not path.startswith(self.public_path + "_") or path in taken:
                path = sub_path(self.public_path, item.get("title") or item["dashboard"], taken)
            taken.add(path)
            views = tuple(str(v) for v in item.get("views") or []) or (ALL_VIEWS,)
            seen = look(str(item["dashboard"]))
            out.append(Panel(path=path, dashboard=str(item["dashboard"]),
                             title=(seen.get("title") or item.get("title") or None), views=views,
                             icon=seen.get("icon")))
        # The Energy dashboard, if Home Assistant has one. Whether the owner
        # keeps it in their own sidebar, or for administrators only, does not
        # matter here: the link publishes it.
        energy = ha_data.panel_look(hass, "energy") if hass is not None else None
        if self.options.get(CONF_ENERGY) and energy is not None:
            out.append(Panel(path=sub_path(self.public_path, "energy", taken), kind="energy",
                             dashboard="energy", title=energy.get("title"), icon=energy.get("icon")))
            taken.add(out[-1].path)
        if self.options.get(CONF_FRIGATE_UI):
            out.append(Panel(path=sub_path(self.public_path, "frigate", taken), kind="frigate", title="Frigate"))
        return out

    @property
    def paths(self) -> dict[str, str]:
        """Real address -> public address, for every panel that has both."""
        return {p.dashboard: p.path for p in self.panels if p.dashboard}

    def panel_at(self, path: str) -> Panel | None:
        return next((p for p in self.panels if p.path == path), None)

    @property
    def published_views(self) -> list[str]:
        return configured_views(self.options, CONF_VIEWS)

    @property
    def support_views(self) -> list[str]:
        return configured_views(self.options, CONF_SUPPORT_VIEWS)

    @property
    def public_title(self) -> str | None:
        return (self.options.get(CONF_TITLE) or "").strip() or None

    @property
    def show_header(self) -> bool:
        """The tab bar is worth showing when there is more than one view, and
        has to stay when there is more than one panel: its menu button is how
        a phone opens the sidebar."""
        if len(self.panels) > 1:
            return True
        return bool(self.options.get(CONF_SHOW_TABS, True)) and len(self.published_views) > 1

    def note_refusal(self, what: str) -> None:
        """Remember something a visitor's page asked for and did not get."""
        if what not in self._refusals:
            self._refusals.append(what)

    def note_refused_type(self, what: str) -> None:
        """Count a message the mirror turned down, by kind."""
        if what in self._refused_types or len(self._refused_types) < 80:
            self._refused_types[what] = self._refused_types.get(what, 0) + 1

    def note_page_error(self, what: str) -> None:
        """Remember an error a visitor's page reported. The text comes from a
        visitor's browser: it is kept short, kept few, and only ever shown to
        the owner."""
        if what not in self._page_errors:
            self._page_errors.append(what)

    def invalidate(self) -> None:
        """Called when the dashboard or the options change: what the views
        allow is worked out again, for the next request and for the pages
        that are open (async_keep_current)."""
        self._access_at = 0.0

    def current_access(self) -> Access:
        """What a visitor may read as last worked out, for a page that is
        open. Nothing at all before the first time."""
        return self._access if self._access is not None else Access(camera_streams=False)

    async def async_keep_current(self, _now: Any = None) -> None:
        """While somebody is looking, keep what they may read up to date: at
        once after a dashboard was edited, and every half minute. Without a
        visitor there is nothing to keep; the next one's first request works
        it out afresh."""
        if getattr(self, "sockets", None) and getattr(self, "active", True):
            try:
                await self.async_access()
            except Exception:  # noqa: BLE001 - tried again in half a minute
                _LOGGER.debug("Could not refresh what the link publishes", exc_info=True)

    # -- what a visitor receives -------------------------------------------------

    def publish(self, config: dict[str, Any], dashboard: str | None = None) -> dict[str, Any]:
        """One of the link's dashboards (the main one unless named) cut down
        to its chosen views, under its public name."""
        panel = next(
            (p for p in self.panels if p.kind == "dashboard" and p.dashboard == dashboard),
            self.panels[0],
        )
        published = publish_config(
            config,
            published=panel.views,
            support=panel.support,
            title=panel.title,
            paths=self.paths,
        )
        if not has_published_view(config, panel.views):
            _LOGGER.warning(
                "None of the published views %s is in dashboard %r (views: %s); publishing nothing",
                list(panel.views),
                panel.dashboard,
                ", ".join(str(v.get("path")) for v in config.get("views") or [] if isinstance(v, dict))
                or "none",
            )
            published["views"] = []
        return published

    async def async_published_config(self) -> dict[str, Any] | None:
        """The published main dashboard, or None when it has no saved configuration."""
        config = await ha_data.async_load_dashboard_config(self.hass, self.dashboard) if self.dashboard else None
        return self.publish(config) if config else None

    async def async_access(self) -> Access:
        """What a visitor may read, reused for a short while (ACCESS_SECONDS)."""
        now = time.monotonic()
        if self._access is None or now - self._access_at > ACCESS_SECONDS:
            before = getattr(self, "_said", None)
            # Marked fresh first: several requests arriving together work it
            # out once, and an edit made meanwhile marks it stale again.
            self._access_at = now
            try:
                access = await self._async_read_access()
            except BaseException:
                self._access_at = 0.0
                raise
            self._access = access
            if before is not None and self._said != before and getattr(self, "sockets", None):
                # The dashboard, the energy settings or a shared card
                # definition changed what this link publishes. Whoever has a
                # page open is hung up on: the page connects again by itself
                # and gets what is published now. Streams, signed requests
                # and subscriptions started under the old rules end with the
                # connection instead of running on.
                _LOGGER.debug("What /%s publishes has changed: reconnecting its visitors", self.public_path)
                await self.async_close_sockets()
        return self._access

    async def _async_read_access(self) -> Access:
        """What the mirrored frontend may read: every entity and statistic the
        sent views refer to, the strings written in them, the cameras they
        show, plus the energy preferences' statistics.

        This scans the views as they are sent rather than a list of known card
        types: in mirror mode the real frontend draws every card, custom ones
        included, so their entities must be readable — but nothing outside the
        sent views is.
        """
        entities: set[str] = set()
        statistics: set[str] = set()
        candidates: set[str] = set()
        strings: set[str] = set()
        conditions: set[str] = set()
        plains: list[dict[str, Any]] = []
        anything = False
        for panel in self.panels:
            if panel.kind != "dashboard" or not panel.dashboard:
                continue
            raw = await ha_data.async_load_dashboard_config(self.hass, panel.dashboard)
            if not raw:
                continue
            # Scanned without the address rewrite: an entity or a template is
            # allowed as the owner wrote it...
            plain = publish_config(raw, published=panel.views, support=panel.support)
            sent = self.publish(raw, panel.dashboard)
            if not sent["views"]:
                continue
            anything = True
            plains.append(plain)
            found = referenced_ids(plain)
            entities |= found[0]
            statistics |= found[1]
            candidates |= found[2]
            # ... and a template is also allowed in the form the visitor's
            # frontend holds it, with the dashboard's address replaced.
            strings |= collect_strings(plain) | collect_strings(sent)
            conditions |= collect_conditions(plain) | collect_conditions(sent)
        energy_panel = any(p.kind == "energy" for p in self.panels)
        if not anything and not energy_panel and not any(p.kind == "frigate" for p in self.panels):
            self._said = ()
            return Access(camera_streams=False)
        # What the dashboards themselves say, before anything that depends on
        # the state of the house is added.
        said: list[Any] = [frozenset(entities), frozenset(statistics), frozenset(candidates)]
        # Cards that name no entities but pick them with filters
        # (auto-entities): what the filters select is published too.
        picked = await self._async_auto_entities(plains)
        self._auto_entities = len(picked - entities)
        entities |= picked
        # UIX foundries are card definitions kept outside the dashboard. The
        # ones a sent view uses are part of that view: their text and their
        # entities count as written there.
        foundries = await ha_data.async_uix_foundries(self.hass)
        used = foundries_used(foundries, strings) if foundries is not None else None
        if used:
            in_foundries = referenced_ids(used)
            entities |= in_foundries[0]
            statistics |= in_foundries[1]
            candidates |= in_foundries[2]
            strings |= collect_strings(used)
            conditions |= collect_conditions(used)
        # Entity ids inside templates count only if such an entity exists.
        entities |= {c for c in candidates if self.hass.states.get(c) is not None}
        # The energy settings are published with the Energy dashboard or an
        # energy card on a sent view, and not otherwise. Energy and power
        # cards name no entities: they draw what the settings list, and need
        # those sensors' live values too.
        shows_energy = energy_panel or any(_ENERGY_CARD.match(value) for value in strings)
        energy: set[str] = set()
        if shows_energy:
            prefs = await ha_data.async_energy_prefs(self.hass)
            energy = ha_data.energy_statistic_ids(prefs)
            statistics |= energy
            entities |= {e for e in energy if self.hass.states.get(e) is not None}
        # Statistics of plain sensors share the entity id.
        statistics |= entities
        self._said = (
            *said, frozenset(strings), frozenset(conditions), frozenset(energy), shows_energy,
            tuple((p.path, p.kind, p.dashboard, p.views, p.support) for p in self.panels),
        )
        return Access(
            energy=shows_energy,
            entities=entities,
            statistics=statistics,
            strings=strings,
            devices=ha_data.device_ids(self.hass, entities),
            camera_streams=bool(self.options.get(CONF_CAMERA_STREAMS, True)),
            frigate=self._frigate_access(entities, strings),
            foundries=set(used) if used is not None else None,
            media=self._listed_media,
            hide_location=bool(self.options.get(CONF_HIDE_LOCATION, True)),
            conditions=conditions,
        )

    async def _async_auto_entities(self, configs: list[dict[str, Any]]) -> set[str]:
        """The entities the auto-entities cards on the published views select
        (autoentities.py). Worked out afresh with the rest of the access, so a
        battery that drops below a card's threshold joins within the half
        minute the access is kept for."""
        cards = [card for config in configs for card in autoentities.cards_in(config)]
        if not cards:
            return set()
        try:
            world = ha_data.auto_entities_world(self.hass)
            picked: set[str] = set()
            for card in cards[:100]:
                template = (card.get("filter") or {}).get("template")
                rendered = (
                    await ha_data.async_render_owner_template(self.hass, template, {"config": card})
                    if isinstance(template, str)
                    else None
                )
                picked |= autoentities.selected(
                    card, world, (lambda _t, _v, r=rendered: r) if rendered is not None else None
                )
            return picked
        except Exception:  # noqa: BLE001 - a list that stays empty is better than no page
            _LOGGER.debug("Could not work out auto-entities filters", exc_info=True)
            return set()

    def _frigate_access(self, entities: set[str], strings: set[str]) -> FrigateAccess | None:
        """Published Frigate cameras: those whose camera entity is on a sent
        view, those named in a card there, and those listed in the options."""
        instances = ha_data.frigate_instances(self.hass)
        if not instances:
            return None
        known: set[str] = set().union(*instances.values())
        cameras = frigate_cameras_in(strings, known)
        cameras |= ha_data.frigate_cameras_of(self.hass, entities) & known
        cameras |= {str(c) for c in self.options.get(CONF_FRIGATE_CAMERAS) or []} & known
        return FrigateAccess(instances=instances, cameras=cameras, events=self._frigate_events)

    # -- compatibility: what versions before 0.6 called -------------------------

    async def async_mirror_allowlists(self) -> tuple[set[str], set[str]]:
        access = await self.async_access()
        return access.entities, access.statistics

    async def async_mirror_templates(self) -> set[str]:
        return (await self.async_access()).strings

    # -- owner-facing ----------------------------------------------------------

    async def async_owner_report(self) -> dict[str, Any]:
        """What a visitor can reach, for diagnostics. Never served publicly."""
        access = await self.async_access()
        sent = await self.async_published_config() or {"views": []}
        expires = self.expires_at
        return {
            "link": {
                "for": self.name,
                "working": self.live,
                "expires_at": expires.isoformat() if expires else None,
                "expired": self.expired,
                "open_connections": len(self.sockets),
                "panels": [
                    {"address": f"/{p.path}", "kind": p.kind, "dashboard": p.dashboard, "views": list(p.views)}
                    for p in self.panels
                ],
            },
            "entities_picked_by_auto_entities_filters": getattr(self, "_auto_entities", 0),
            "dashboard": self.dashboard,
            "views_as_tabs": [
                str(v.get("path") or "(first view)") for v in sent["views"] if v.get("visible", True) is not False
            ],
            "views_without_a_tab": [
                str(v.get("path") or "(first view)") for v in sent["views"] if v.get("visible", True) is False
            ],
            "entity_allowlist": sorted(access.entities),
            "statistic_allowlist": sorted(access.statistics),
            "strings_allowed_as_templates": len(access.strings),
            "live_camera_streams": access.camera_streams,
            "frigate_cameras": sorted(access.frigate.cameras) if access.frigate else "(Frigate not found)",
            "frigate_events_remembered": len(self._frigate_events),
            "uix_foundries": sorted(access.foundries) if access.foundries is not None else "(UIX not found)",
            "home_location_hidden": access.hide_location,
            # If a card stays empty on the public page, the reason is usually here.
            "recently_refused": list(self._refusals),
            # Every kind of request turned down, with how often. A visitor
            # pressing a button shows here as call_service; that is the page
            # being read-only, not a fault.
            "refused_requests": dict(sorted(self._refused_types.items())),
            # What went wrong inside visitors' pages: a card that threw, a
            # script that did not load, an error card and what it said.
            "page_errors": list(self._page_errors),
        }
