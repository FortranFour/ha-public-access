"""Mirror mode: the real Home Assistant frontend, behind bulletproof glass.

The visitor gets Home Assistant's own frontend — the same JavaScript, the same
cards, custom cards, animations, whatever layout the owner chose — so nothing is
re-implemented and nothing is assumed. What changes is where its websocket goes:
not to Home Assistant, but to this module, which pretends to be Home Assistant
and forwards only what a read-only viewer may do.

Two independent layers make it read-only:

1. **Allowlist.** Only the message types listed below are forwarded. Service
   calls, saves, scripts, templates and everything else are answered with an
   error and never reach Home Assistant.
2. **Permissions.** The forwarded messages run as a system user in Home
   Assistant's own `system-read-only` group, so even a message that slipped past
   the allowlist would be refused by Home Assistant itself.

And reading is narrowed to what the owner published: the chosen views of one
dashboard (sanitize.publish_config), the entities written in them, and — for
pictures, video and Frigate — what policy.Access allows. A few allowed types
could read far more than a dashboard if forwarded as they are (`auth/sign_path`
signs any path, `media_source/resolve_media` opens any file, the Frigate
messages list any camera); each of those is checked here first.

No real token exists in the browser: the page seeds the frontend with a fake one,
and this endpoint accepts it because it authenticates nobody — it is public by
design.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from aiohttp import WSMsgType, web

from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import RefreshToken, User
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .policy import CHECKED_TEMPLATE_SECONDS, MAX_SIGN_SECONDS, Access, local_path
from .sanitize import pin_default_panel, public_core_config, public_devices

_LOGGER = logging.getLogger(__name__)

from .const import MAX_PUBLIC_SESSIONS

STORAGE_VERSION = 1
STORAGE_KEY = "public_access.viewer"
VIEWER_NAME = "Public Access viewer"

# Concurrent public websocket connections, in total. The view refuses extra
# visitors before the upgrade; this is the backstop behind it.
MAX_CONNECTIONS = MAX_PUBLIC_SESSIONS

# What the frontend needs to draw a dashboard and nothing more. Every type not
# listed is refused. Types marked "filtered" are rewritten so the visitor only
# sees the entities and statistics the published views use.
ALLOWED_TYPES: frozenset[str] = frozenset(
    {
        "ping",
        "supported_features",
        "auth/current_user",
        "get_config",
        "get_services",
        "get_panels",  # rewritten: only the published dashboard exists
        "get_states",  # filtered
        "subscribe_entities",  # filtered
        "subscribe_events",  # limited to harmless event types
        "unsubscribe_events",
        "frontend/get_themes",
        "frontend/get_translations",
        "frontend/get_user_data",
        "frontend/subscribe_user_data",  # the viewer's own (empty) preferences
        "frontend/subscribe_system_data",  # frontend feature flags, nothing private
        "frontend/get_icons",
        "brands/access_token",  # lets the frontend fetch integration logos
        "labs/subscribe",  # experimental-feature flags
        # Deliberately absent: repairs/list_issues and persistent_notification/*
        # would leak the owner's repair issues and notifications.
        "config/area_registry/list",
        "config/floor_registry/list",
        "config/label_registry/list",
        "config/device_registry/list",  # filtered: published entities' devices, no hardware ids
        "config/entity_registry/list",  # filtered
        "config/entity_registry/list_for_display",  # filtered
        "config/entity_registry/get",  # a published entity only
        "config/entity_registry/get_entries",  # filtered
        "lovelace/config",  # rewritten: the published dashboard, the chosen views
        "lovelace/dashboards/list",  # answered here: only the published dashboard exists
        "lovelace/info",  # resource mode; the frontend asks at boot
        "lovelace/resources",  # custom cards need their JavaScript
        # render_template as it is for a template string that appears verbatim
        # in a published view (the markdown card renders its content through
        # it). A template some card assembled in the browser is rendered under
        # watch: its result is passed on only if it read published entities
        # and nothing else (policy.Access.template_trust). Anything else
        # could read every state and is refused.
        "render_template",
        # UIX (UI eXtension): its frontend waits for this subscription before
        # it draws anything it forges (cards, badges, rows), and receives its
        # shared templates ("foundries") through it. Its messages that change
        # something (uix/set_foundry and the like) stay refused.
        "uix/connect",
        "uix/get_foundries",
        "energy/get_prefs",
        "energy/info",
        "energy/validate",
        "energy/solar_forecast",
        "energy/fossil_energy_consumption",
        "recorder/info",
        "recorder/list_statistic_ids",
        "recorder/get_statistics_metadata",
        "recorder/statistics_during_period",
        "recorder/statistic_during_period",
        "history/history_during_period",  # filtered
        "history/stream",  # filtered
        "logbook/get_events",  # filtered, and without who did what
        "logbook/event_stream",  # filtered, and without who did what
        "weather/subscribe_forecast",  # a published entity only
        "calendar/event/subscribe",  # a published calendar only
        # Cameras: only for a camera entity on a published view.
        "camera/capabilities",
        "camera/get_prefs",
        "camera/stream",
        "camera/webrtc/get_client_config",
        "camera/webrtc/offer",
        "camera/webrtc/candidate",
        # A short-lived link to one picture or clip, and only for a path the
        # policy allows (policy.Access.path_allowed). Signing anything else
        # would hand the visitor Home Assistant's whole read API.
        "auth/sign_path",
        "media_source/resolve_media",  # only media the owner wrote into a view
        # Frigate (the Frigate integration's own messages): reading events
        # and recordings of published cameras. "frigate/event/retain" and
        # "frigate/reviews/viewed" change something and stay refused.
        "frigate/events/get",
        "frigate/events/summary",
        "frigate/events/subscribe",
        "frigate/events/unsubscribe",
        "frigate/reviews/get",
        "frigate/reviews/subscribe",
        "frigate/reviews/unsubscribe",
        "frigate/recordings/get",
        "frigate/recordings/summary",
        "frigate/ptz/info",
        "sensor/numeric_device_classes",
        "manifest/list",
        "manifest/get",
    }
)

ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "state_changed",
        "core_config_updated",
        "themes_updated",
        "panels_updated",
        "lovelace_updated",
        "component_loaded",
        "entity_registry_updated",
        "area_registry_updated",
    }
)

# Frontend preference stores. Forwarded, but any default_panel in them is
# rewritten to the public path (see sanitize.pin_default_panel).
FRONTEND_DATA_TYPES: frozenset[str] = frozenset(
    {"frontend/get_user_data", "frontend/subscribe_user_data", "frontend/subscribe_system_data"}
)

CAMERA_TYPES: frozenset[str] = frozenset(
    {
        "camera/capabilities",
        "camera/get_prefs",
        "camera/stream",
        "camera/webrtc/get_client_config",
        "camera/webrtc/offer",
        "camera/webrtc/candidate",
    }
)
# Starting one of these makes Home Assistant open a live stream.
CAMERA_STREAM_TYPES: frozenset[str] = frozenset(
    {"camera/stream", "camera/webrtc/offer", "camera/webrtc/candidate"}
)
FRIGATE_ANSWER_TYPES: frozenset[str] = frozenset(
    {"frigate/events/get", "frigate/events/summary", "frigate/reviews/get"}
)
FRIGATE_PUSH_TYPES: frozenset[str] = frozenset(
    {"frigate/events/subscribe", "frigate/reviews/subscribe"}
)
LOGBOOK_TYPES: frozenset[str] = frozenset({"logbook/get_events", "logbook/event_stream"})
STATISTIC_LIST_TYPES: frozenset[str] = frozenset(
    {"recorder/list_statistic_ids", "recorder/get_statistics_metadata"}
)

# Answered with an empty, successful result rather than forwarded or refused.
QUIET_TYPES: dict[str, Any] = {
    "persistent_notification/subscribe": None,
    "repairs/list_issues": {"issues": []},
    "uix/log": None,  # a card's note for the owner's log: dropped
}

_connections = 0


async def async_get_viewer(hass: HomeAssistant) -> tuple[User, RefreshToken]:
    """The read-only system user the mirrored connection runs as.

    Created once and remembered; a refresh token is needed only because Home
    Assistant's connection object wants one for its audit context.
    """
    store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    user = None
    if user_id := data.get("user_id"):
        user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_active:
        user = await hass.auth.async_create_system_user(
            VIEWER_NAME, group_ids=[GROUP_ID_READ_ONLY]
        )
        await store.async_save({"user_id": user.id})
        _LOGGER.info("Created the read-only viewer user %s", user.id)
    token = next(iter(user.refresh_tokens.values()), None)
    if token is None:
        token = await hass.auth.async_create_refresh_token(
            user, client_name="Public Access mirror"
        )
    return user, token


# The page scripts and the frontend glue (the "lovelace" alias panel, silent
# subscriptions) live in mirror_core.py. Everything that decides what a visitor
# may reach stays here and runs before that glue sees a message.


class MirrorSession:
    """One visitor's websocket, proxied through a read-only viewer connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        ws: web.WebSocketResponse,
        user: User,
        token: RefreshToken,
        remote: str | None,
        *,
        public_path: str,
        dashboard: str,
        access: Access,
        publish: Callable[[dict[str, Any]], dict[str, Any]],
        title: str | None = None,
        note: Callable[[str], None] | None = None,
        core: Any,
    ) -> None:
        from homeassistant.components.websocket_api.connection import ActiveConnection
        from homeassistant.components.websocket_api.messages import (
            error_message,
        )

        self._hass = hass
        self._ws = ws
        self._public_path = public_path
        self._dashboard = dashboard
        self._access = access
        self._entities = access.entities
        self._statistics = access.statistics
        self._templates = access.strings
        self._publish = publish
        self._title = title
        self._note = note or (lambda _what: None)
        self._types: dict[int, str] = {}
        # render_template subscriptions whose results are checked before
        # they are passed on, with their text for the owner's diagnostics.
        self._watched: dict[int, str] = {}
        # The frontend glue, called only after this class's own filters.
        self._core = core.Session(public_path=public_path, dashboard=dashboard)
        self._loop = hass.loop
        self._error = error_message
        self._connection = ActiveConnection(
            _LOGGER, hass, self._send, user, token, remote
        )

    # -- outbound (Home Assistant -> visitor) ---------------------------------

    def _send(self, message: bytes | str | dict[str, Any]) -> None:
        if isinstance(message, (bytes, str)):
            raw = message.decode() if isinstance(message, bytes) else message
            try:
                decoded = json.loads(raw)
            except ValueError:
                self._loop.create_task(self._ws.send_str(raw))
                return
            message = decoded
        message = self._filter_outbound(message)
        if message is not None:
            # Filters first, glue second: the glue only adapts what may be seen.
            message = self._core.outbound(self._types.get(message.get("id"), ""), message)
            self._loop.create_task(self._ws.send_str(json.dumps(message, default=str)))

    def _filter_outbound(self, message: dict[str, Any]) -> dict[str, Any] | None:
        msg_id = message.get("id")
        kind = self._types.get(msg_id, "")
        if message.get("type") == "result" and message.get("success") is False:
            # Diagnostics: an error Home Assistant itself returned to a forwarded
            # message. These surface in the page as unhandled rejections.
            _LOGGER.debug("Mirror: HA answered %s with %s", kind, message.get("error"))
            return message
        if msg_id in self._watched and message.get("type") == "event":
            return self._watched_template_event(msg_id, message)
        if kind in FRONTEND_DATA_TYPES:
            # Results for get_*, events for subscribe_*: both carry the data.
            pin_default_panel(message.get("result"), self._public_path)
            pin_default_panel(message.get("event"), self._public_path)
            return message
        if message.get("type") == "result" and kind in FRIGATE_ANSWER_TYPES:
            # Frigate answers in JSON text, so this comes before the checks
            # on lists and dicts below.
            message["result"] = self._access.frigate_answer(kind, message.get("result"))
        elif message.get("type") == "result" and isinstance(message.get("result"), (list, dict)):
            result = message["result"]
            if kind == "get_states":
                message["result"] = [
                    s for s in result if self._entity_allowed(s.get("entity_id"))
                ]
            elif kind in ("config/entity_registry/list",):
                message["result"] = [
                    e for e in result if self._entity_allowed(e.get("entity_id"))
                ]
            elif kind == "config/entity_registry/list_for_display" and isinstance(result, dict):
                result["entities"] = [
                    e for e in result.get("entities", []) if self._entity_allowed(e.get("ei"))
                ]
            elif kind == "config/entity_registry/get_entries" and isinstance(result, dict):
                message["result"] = {
                    k: v for k, v in result.items() if self._entity_allowed(k)
                }
            elif kind == "config/device_registry/list" and isinstance(result, list):
                message["result"] = public_devices(result, self._access.devices)
            elif kind == "get_config" and isinstance(result, dict):
                message["result"] = public_core_config(result, self._title)
            elif kind in STATISTIC_LIST_TYPES and isinstance(result, list):
                message["result"] = [
                    s for s in result
                    if isinstance(s, dict) and s.get("statistic_id") in self._statistics
                ]
            elif kind == "logbook/get_events" and isinstance(result, list):
                message["result"] = [self._logbook_entry(e) for e in result if isinstance(e, dict)]
            elif kind == "get_panels" and isinstance(result, dict):
                message["result"] = self._panels(result)
            elif kind == "lovelace/config" and isinstance(result, dict):
                message["result"] = self._published(result)
            elif kind == "auth/current_user" and isinstance(result, dict):
                result["is_admin"] = False
                result["is_owner"] = False
        elif message.get("type") == "event" and kind == "subscribe_entities":
            event = message.get("event") or {}
            for bucket in ("a", "c", "r"):
                if isinstance(event.get(bucket), dict):
                    event[bucket] = {
                        k: v for k, v in event[bucket].items() if self._entity_allowed(k)
                    }
                elif isinstance(event.get(bucket), list):
                    event[bucket] = [k for k in event[bucket] if self._entity_allowed(k)]
        elif message.get("type") == "event" and kind == "subscribe_events":
            event = message.get("event") or {}
            data = event.get("data") or {}
            if "entity_id" in data and not self._entity_allowed(data.get("entity_id")):
                return None
            if event.get("event_type") == "lovelace_updated":
                # Only the published dashboard exists for the visitor, under
                # its public name; edits to any other are nobody's business.
                if data.get("url_path") != self._dashboard:
                    return None
                event["data"] = {**data, "url_path": self._public_path}
            elif event.get("event_type") == "core_config_updated":
                # The frontend reads the (filtered) configuration again; the
                # changed values themselves, location included, are not sent.
                event["data"] = {}
        elif message.get("type") == "event" and kind in FRIGATE_PUSH_TYPES:
            checked = self._access.frigate_event(message.get("event"))
            if checked is None:
                return None
            message["event"] = checked
        elif message.get("type") == "event" and kind == "logbook/event_stream":
            event = message.get("event")
            if isinstance(event, dict) and isinstance(event.get("events"), list):
                event["events"] = [self._logbook_entry(e) for e in event["events"] if isinstance(e, dict)]
        return message

    def _watched_template_event(self, msg_id: int, message: dict[str, Any]) -> dict[str, Any]:
        """A result of a template that was not written in a published view.

        Home Assistant says with every result which entities the template
        read. If all of them are published the result goes to the visitor.
        If not, the result is withheld, the subscription is ended, and the
        page is told the template failed."""
        event = message.get("event")
        if not isinstance(event, dict) or "result" not in event:
            return message  # a template error, reported as Home Assistant words it
        listeners = event.get("listeners")
        if self._access.template_reads_allowed(listeners):
            # Home Assistant hands these over as sets; a list is what the page
            # gets from Home Assistant itself.
            event["listeners"] = {
                key: sorted(value) if isinstance(value, (set, frozenset)) else value
                for key, value in listeners.items()
            }
            return message
        read = listeners if isinstance(listeners, dict) else {}
        outside = sorted(
            e for e in read.get("entities") or [] if not self._entity_allowed(e)
        )
        if read.get("all"):
            why = "it goes through every entity"
        elif read.get("domains"):
            why = "it goes through every entity of " + ", ".join(sorted(read["domains"]))
        elif outside:
            why = "it reads " + ", ".join(outside[:8]) + ", not on a published view"
        else:
            why = "Home Assistant did not say what it read"
        self._note(f"Template not rendered for a visitor: {why}. Template: {self._watched[msg_id][:300]}")
        _LOGGER.debug("Mirror withheld a template result (%s)", why)
        self._end_subscription(msg_id)
        return {
            "id": msg_id,
            "type": "event",
            "event": {"error": "Read-only public view: this template reads what is not published", "level": "ERROR"},
        }

    def _end_subscription(self, msg_id: int) -> None:
        """Stop a forwarded subscription from this side."""
        self._watched.pop(msg_id, None)
        unsubscribe = self._connection.subscriptions.pop(msg_id, None)
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001 - ending it must not end the visit
                _LOGGER.debug("Could not end subscription %s", msg_id, exc_info=True)

    def _entity_allowed(self, entity_id: Any) -> bool:
        if self._entities is None:
            return True
        return isinstance(entity_id, str) and entity_id in self._entities

    @staticmethod
    def _logbook_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """A logbook line without who or what caused it: the owner's users
        and automations are not the visitor's business."""
        return {k: v for k, v in entry.items() if not k.startswith("context_")}

    def _panels(self, panels: dict[str, Any]) -> dict[str, Any]:
        """Only one panel exists for the visitor: the published dashboard, under
        the public path. The frontend then asks for lovelace/config with that
        url_path, which is rewritten back to the real dashboard."""
        source = panels.get(self._dashboard) or {}

        def panel(url_path: str) -> dict[str, Any]:
            return {
                "component_name": "lovelace",
                "icon": source.get("icon"),
                "title": self._title or source.get("title") or "Dashboard",
                "config": {"mode": "storage"},
                "url_path": url_path,
                "require_admin": False,
                "config_panel_domain": None,
            }

        return {self._public_path: panel(self._public_path)}

    def _published(self, config: dict[str, Any]) -> dict[str, Any]:
        """Publish exactly the chosen views; the others are not even sent to the
        browser (sanitize.publish_config).

        If the configured views are gone (renamed, deleted), publish nothing
        rather than the first view: that one was never chosen and may be private."""
        return self._publish(config)

    # -- inbound (visitor -> Home Assistant) ----------------------------------

    def _reply_ok(self, msg_id: int, result: Any = None) -> None:
        self._loop.create_task(
            self._ws.send_str(
                json.dumps({"id": msg_id, "type": "result", "success": True, "result": result})
            )
        )

    def _refuse(self, msg_id: Any, kind: Any, why: str = "") -> None:
        self._loop.create_task(
            self._ws.send_str(
                json.dumps(self._error(msg_id or 0, "unauthorized", "Read-only public view"))
            )
        )
        _LOGGER.debug("Mirror refused %s %s", kind, why)

    def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        msg_id = msg.get("id")

        # Requests that must not be forwarded but where "nothing" is the honest
        # answer for a viewer: no notifications, no repair issues, and the
        # frontend's own error reports are simply dropped. Answering these with
        # an error instead leaves unhandled rejections in the frontend.
        if isinstance(msg_id, int) and (
            kind in QUIET_TYPES
            or (kind == "call_service" and msg.get("domain") == "system_log")
        ):
            result = QUIET_TYPES.get(kind)
            if isinstance(kind, str) and kind.endswith("/subscribe"):
                # Remembered, so that cancelling it later is answered here too
                # instead of reaching Home Assistant, which never heard of it.
                self._core.silent_subscription(msg_id)
            self._loop.create_task(
                self._ws.send_str(
                    json.dumps({"id": msg_id, "type": "result", "success": True, "result": result})
                )
            )
            return

        if not isinstance(msg_id, int) or kind not in ALLOWED_TYPES:
            self._refuse(
                msg_id,
                kind,
                f"{msg.get('domain')}.{msg.get('service')}" if kind == "call_service" else "",
            )
            return

        if kind == "unsubscribe_events":
            self._watched.pop(msg.get("subscription"), None)
        answered, result = self._core.local_answer(kind, msg)
        if answered:
            self._reply_ok(msg_id, result)
            return

        if kind == "lovelace/dashboards/list":
            # Never forwarded: the owner's other dashboards do not exist here.
            self._reply_ok(msg_id, self._core.dashboards(self._title))
            return
        if kind == "subscribe_events":
            if msg.get("event_type", "*") not in ALLOWED_EVENT_TYPES:
                # Never forwarded. How the frontend is told (an error, or a
                # subscription that never fires) is the glue's business.
                self._reply_ok(msg_id, self._core.silent_subscription(msg_id))
                return
        elif kind == "subscribe_entities" and self._entities is not None:
            msg = {**msg, "entity_ids": sorted(self._entities)}
        elif kind in ("history/history_during_period", "history/stream"):
            wanted = msg.get("entity_ids") or []
            msg = {**msg, "entity_ids": [e for e in wanted if self._entity_allowed(e)]}
            if not msg["entity_ids"]:
                self._loop.create_task(self._ws.send_str(json.dumps({"id": msg_id, "type": "result", "success": True, "result": {}})))
                return
        elif kind in LOGBOOK_TYPES:
            # By entity only, and only published ones; a search by device or
            # across everything would list the whole house.
            wanted = msg.get("entity_ids") or []
            allowed = [e for e in wanted if self._entity_allowed(e)]
            if not allowed:
                if kind == "logbook/get_events":
                    self._reply_ok(msg_id, [])
                else:
                    self._reply_ok(msg_id, self._core.silent_subscription(msg_id))
                return
            msg = {k: v for k, v in msg.items() if k not in ("device_ids", "context_id")}
            msg["entity_ids"] = allowed
        elif kind == "recorder/statistics_during_period" and self._statistics is not None:
            wanted = msg.get("statistic_ids") or []
            msg = {**msg, "statistic_ids": [s for s in wanted if s in self._statistics]}
        elif kind == "recorder/statistic_during_period":
            if msg.get("statistic_id") not in self._statistics:
                self._refuse(msg_id, kind, "(statistic not on a published view)")
                return
        elif kind == "recorder/get_statistics_metadata":
            wanted = msg.get("statistic_ids") or sorted(self._statistics)
            msg = {**msg, "statistic_ids": [s for s in wanted if s in self._statistics]}
        elif kind in ("weather/subscribe_forecast", "calendar/event/subscribe"):
            if not self._entity_allowed(msg.get("entity_id")):
                self._refuse(msg_id, kind, "(entity not on a published view)")
                return
        elif kind == "config/entity_registry/get":
            if not self._entity_allowed(msg.get("entity_id")):
                self._refuse(msg_id, kind, "(entity not on a published view)")
                return
        elif kind == "config/entity_registry/get_entries":
            wanted = msg.get("entity_ids") or []
            msg = {**msg, "entity_ids": [e for e in wanted if self._entity_allowed(e)]}
        elif kind in CAMERA_TYPES:
            entity_id = msg.get("entity_id")
            if not (isinstance(entity_id, str) and entity_id.startswith("camera.")
                    and self._entity_allowed(entity_id)):
                self._refuse(msg_id, kind, "(camera not on a published view)")
                return
            if not self._access.camera_streams:
                if kind == "camera/capabilities":
                    # Still pictures only: the frontend then asks for no stream.
                    self._reply_ok(msg_id, {"frontend_stream_types": []})
                    return
                if kind in CAMERA_STREAM_TYPES:
                    self._refuse(msg_id, kind, "(live streams are switched off)")
                    return
        elif kind == "auth/sign_path":
            path = local_path(msg.get("path"))
            msg = {**msg, "path": path}
            if self._access.stream_info_allowed(path):
                # A stream's description names the camera's address at home,
                # so the page is pointed at the public endpoint, which serves
                # a cleaned copy, rather than given a link to the original.
                self._reply_ok(msg_id, {"path": f"/{self._public_path}{path}"})
                return
            if not self._access.path_allowed(path):
                self._note(f"Link not signed for a visitor (not published): {str(path)[:200]}")
                self._refuse(msg_id, kind, "(path not published)")
                return
            expires = msg.get("expires")
            if isinstance(expires, int) and expires > MAX_SIGN_SECONDS:
                msg = {**msg, "expires": MAX_SIGN_SECONDS}
        elif kind == "media_source/resolve_media":
            if not self._access.media_allowed(msg.get("media_content_id")):
                self._refuse(msg_id, kind, "(media not on a published view)")
                return
        elif kind.startswith("frigate/"):
            checked = self._access.frigate_request(kind, msg)
            if checked is None:
                self._note(
                    f"Frigate request not passed on for a visitor (camera not published): {kind}"
                    f" {msg.get('camera') or msg.get('cameras') or ''}"
                )
                self._refuse(msg_id, kind, "(Frigate camera not published)")
                return
            msg = checked
        elif kind == "lovelace/config":
            msg = {**msg, "url_path": self._dashboard}
        elif kind == "render_template":
            trust = self._access.template_trust(msg.get("template"))
            if trust is None:
                self._note(
                    "Template not rendered for a visitor: it is not written in a published view"
                    " and uses a function that lists or locates what is not published."
                    f" Template: {str(msg.get('template'))[:300]}"
                )
                self._refuse(msg_id, kind, "(not from a published view)")
                return
            if trust == "checked":
                # Rendered under watch (see _watched_template_event), and
                # with a limit on how long it may take.
                given = msg.get("timeout")
                limit = (
                    min(given, CHECKED_TEMPLATE_SECONDS)
                    if isinstance(given, (int, float)) and given > 0
                    else CHECKED_TEMPLATE_SECONDS
                )
                msg = {**msg, "timeout": limit}
                self._watched[msg_id] = msg["template"]

        self._types[msg_id] = kind
        self._connection.async_handle(self._core.inbound(kind, msg))

    def close(self) -> None:
        self._connection.async_handle_close()


async def async_serve_websocket(
    hass: HomeAssistant,
    request: web.Request,
    *,
    public_path: str,
    dashboard: str,
    access: Access,
    publish: Callable[[dict[str, Any]], dict[str, Any]],
    title: str | None = None,
    note: Callable[[str], None] | None = None,
) -> web.WebSocketResponse:
    """The public websocket endpoint."""
    global _connections  # noqa: PLW0603

    ws = web.WebSocketResponse(heartbeat=55)
    await ws.prepare(request)

    if _connections >= MAX_CONNECTIONS:
        await ws.send_str(json.dumps({"type": "auth_invalid", "message": "Too many viewers"}))
        await ws.close()
        return ws

    from homeassistant.const import __version__

    await ws.send_str(json.dumps({"type": "auth_required", "ha_version": __version__}))
    first = await ws.receive()
    if first.type != WSMsgType.TEXT or json.loads(first.data).get("type") != "auth":
        await ws.close()
        return ws
    await ws.send_str(json.dumps({"type": "auth_ok", "ha_version": __version__}))

    from . import assets

    core = assets.mirror_core()
    if core is None:
        await ws.send_str(json.dumps({"type": "auth_invalid", "message": "Mirror mode is not available"}))
        await ws.close()
        return ws

    user, token = await async_get_viewer(hass)
    session = MirrorSession(
        hass, ws, user, token, request.remote,
        public_path=public_path, dashboard=dashboard, access=access,
        publish=publish, title=title, note=note, core=core,
    )
    _connections += 1
    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    session.handle(item)
    finally:
        _connections -= 1
        session.close()
    return ws


async def async_render_page(
    hass: HomeAssistant,
    public_path: str,
    loading_background: str | None = None,
    show_header: bool = False,
) -> str | None:
    """Home Assistant's own index page, with the page scripts put in place."""
    from homeassistant.components import frontend

    from . import assets

    core = assets.mirror_core()
    if core is None:
        return None

    view = None
    for resource in hass.http.app.router.resources():
        if isinstance(resource, frontend.IndexView):
            view = resource
            break
    if view is None:
        raise RuntimeError("frontend index is not available")

    template = view._template_cache or await hass.async_add_executor_job(view.get_template)  # noqa: SLF001
    html = template.render(
        theme_color=frontend.MANIFEST_JSON["theme_color"],
        extra_modules=hass.data[frontend.DATA_EXTRA_MODULE_URL].urls,
        extra_js_es5=hass.data[frontend.DATA_EXTRA_JS_URL_ES5].urls,
    )
    return core.assemble_page(html, public_path, loading_background, show_header)
