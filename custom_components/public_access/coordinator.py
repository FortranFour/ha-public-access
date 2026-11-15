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
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import data as ha_data
from .const import (
    CONF_CAMERA_STREAMS,
    CONF_DASHBOARD,
    CONF_FRIGATE_CAMERAS,
    CONF_HIDE_LOCATION,
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
    collect_strings,
    configured_views,
    has_published_view,
    publish_config,
    referenced_ids,
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
        # What visitors' pages asked for and did not get, for the owner's
        # diagnostics: the latest few, each kind of refusal once.
        self._refusals: deque[str] = deque(maxlen=40)

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
        """The tab bar is worth showing when there is more than one view."""
        return bool(self.options.get(CONF_SHOW_TABS, True)) and len(self.published_views) > 1

    def note_refusal(self, what: str) -> None:
        """Remember something a visitor's page asked for and did not get."""
        if what not in self._refusals:
            self._refusals.append(what)

    def invalidate(self) -> None:
        """Called when the dashboard or the options change: what the views
        allow is worked out again for the next request."""
        self._access = None

    # -- what a visitor receives -------------------------------------------------

    def publish(self, config: dict[str, Any]) -> dict[str, Any]:
        """The dashboard cut down to the chosen views, under its public name."""
        published = publish_config(
            config,
            published=self.published_views,
            support=self.support_views,
            dashboard=self.dashboard,
            public_path=self.public_path,
            title=self.public_title,
        )
        if not has_published_view(config, self.published_views):
            _LOGGER.warning(
                "None of the published views %s is in dashboard %r (views: %s); publishing nothing",
                self.published_views,
                self.dashboard,
                ", ".join(str(v.get("path")) for v in config.get("views") or [] if isinstance(v, dict))
                or "none",
            )
            published["views"] = []
        return published

    async def async_published_config(self) -> dict[str, Any] | None:
        """The published dashboard, or None when it has no saved configuration."""
        config = await ha_data.async_load_dashboard_config(self.hass, self.dashboard) if self.dashboard else None
        return self.publish(config) if config else None

    async def async_access(self) -> Access:
        """What a visitor may read, reused for a short while (ACCESS_SECONDS)."""
        now = time.monotonic()
        if self._access is None or now - self._access_at > ACCESS_SECONDS:
            self._access = await self._async_read_access()
            self._access_at = now
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
        dashboard = self.dashboard
        raw = await ha_data.async_load_dashboard_config(self.hass, dashboard) if dashboard else None
        if not raw:
            return Access(camera_streams=False)
        # Scanned without the address rewrite: an entity or a template is
        # allowed as the owner wrote it...
        plain = publish_config(raw, published=self.published_views, support=self.support_views)
        sent = self.publish(raw)
        if not sent["views"]:
            return Access(camera_streams=False)
        entities, statistics, candidates = referenced_ids(plain)
        # ... and a template is also allowed in the form the visitor's
        # frontend holds it, with the dashboard's address replaced.
        strings = collect_strings(plain) | collect_strings(sent)
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
        # Entity ids inside templates count only if such an entity exists.
        entities |= {c for c in candidates if self.hass.states.get(c) is not None}
        prefs = await ha_data.async_energy_prefs(self.hass)
        energy = ha_data.energy_statistic_ids(prefs)
        statistics |= energy
        if any(_ENERGY_CARD.match(value) for value in strings):
            # Energy and power cards name no entities: they draw what the
            # energy settings list, and need those sensors' live values too.
            entities |= {e for e in energy if self.hass.states.get(e) is not None}
        # Statistics of plain sensors share the entity id.
        statistics |= entities
        return Access(
            entities=entities,
            statistics=statistics,
            strings=strings,
            devices=ha_data.device_ids(self.hass, entities),
            camera_streams=bool(self.options.get(CONF_CAMERA_STREAMS, True)),
            frigate=self._frigate_access(entities, strings),
            foundries=set(used) if used is not None else None,
            media=self._listed_media,
            hide_location=bool(self.options.get(CONF_HIDE_LOCATION, True)),
        )

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
        return {
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
        }
