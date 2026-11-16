"""Constants for the Public Access integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "public_access"

CONF_DASHBOARD: Final = "dashboard"
CONF_VIEW_PATH: Final = "view_path"  # one view; versions before 0.6
CONF_VIEWS: Final = "views"  # the views shown as tabs
CONF_SUPPORT_VIEWS: Final = "support_views"  # sent but hidden from the tab bar
CONF_SHOW_TABS: Final = "show_tabs"
CONF_TITLE: Final = "title"
CONF_CAMERA_STREAMS: Final = "camera_streams"
CONF_FRIGATE_CAMERAS: Final = "frigate_cameras"
CONF_HIDE_LOCATION: Final = "hide_location"
CONF_PUBLIC_PATH: Final = "public_path"
CONF_ENABLED: Final = "enabled"
CONF_NOINDEX: Final = "noindex"
CONF_CACHE_SECONDS: Final = "cache_seconds"
CONF_FRAME_ANCESTORS: Final = "frame_ancestors"
CONF_LOADING_BACKGROUND: Final = "loading_background"


DEFAULT_PUBLIC_PATH: Final = "public"
DEFAULT_CACHE_SECONDS: Final = 300
DEFAULT_NOINDEX: Final = True

# Requests per minute per client IP for the public endpoints.
RATE_LIMIT_PER_MINUTE: Final = 60
# Pictures and clips fetched by cards (a camera wall, a gallery of Frigate
# thumbnails) come many to a page, so they are counted on their own.
MEDIA_RATE_LIMIT_PER_MINUTE: Final = 600

# Paths the public route must never claim. A registered aiohttp view outranks the
# frontend catch-all resource (verified on HA 2026.9.3), so claiming one of these
# would shadow part of the user's own Home Assistant. The live panel list from
# hass.data[frontend.DATA_PANELS] is checked on top of this static list, since it
# also contains every storage dashboard.
RESERVED_PATHS: Final[frozenset[str]] = frozenset(
    {
        "api",
        "auth",
        "static",
        "local",
        "frontend_latest",
        "frontend_es5",
        "service_worker.js",
        "sw-modern.js",
        "sw-legacy.js",
        "manifest.json",
        "robots.txt",
        "favicon.ico",
        "hacsfiles",
        "media",
        "image",
        "images",
        "onboarding.html",
        "redirect",
        "_my_redirect",
        "notfound",
        "lovelace",
        "profile",
        "config",
        "developer-tools",
        "history",
        "logbook",
        "energy",
        "map",
        "todo",
        "calendar",
        "media-browser",
        "home",
        "security",
        "climate",
        "light",
        "maintenance",
    }
)


# Concurrent public websocket sessions, in total and per visitor address. Each
# one is a live connection on the owner's instance; past the cap new visitors
# get 503 + Retry-After instead of adding load.
MAX_PUBLIC_SESSIONS = 25
MAX_SESSIONS_PER_CLIENT = 4
# Live camera streams open at once, in total and per visitor address. A page
# of camera cards opens one for each camera it shows.
MAX_PUBLIC_STREAMS = 96
MAX_STREAMS_PER_CLIENT = 24
