"""What the mirror may show, decided in the open.

The checks the public connection relies on, kept apart from the proxy so they
can be read and tested on their own: which views of the dashboard are the
published ones, what the dashboard looks like once it is cut down to them, and
which of the owner's details (default dashboard, home location, device
hardware) stay away from visitors. Nothing here reads or writes Home Assistant
state, and nothing here imports Home Assistant.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Chosen in the setup form for a first view that has no address of its own.
FIRST_VIEW = "__first__"


def view_matches(view: dict[str, Any], view_path: str | None) -> bool:
    """Is this the view the owner chose to publish?

    The option is always text, but a view path written in YAML as a bare number
    (``path: 123456``) arrives as an int, so both sides are compared as text. A
    view without a path never matches a configured one.
    """
    if view_path is None:
        return True
    path = view.get("path")
    return path is not None and str(path) == str(view_path)


def configured_views(options: dict[str, Any], key: str = "views") -> list[str]:
    """The views the owner chose, as a list of names.

    Entries made before several views could be published stored one
    ``view_path`` (empty for the first view); they keep working unchanged.
    """
    chosen = options.get(key)
    names = (
        [str(item) for item in chosen if item not in (None, "")]
        if isinstance(chosen, (list, tuple))
        else []
    )
    if names or key != "views":
        return names
    legacy = options.get("view_path")
    return [str(legacy)] if legacy not in (None, "") else [FIRST_VIEW]


def view_selected(view: dict[str, Any], index: int, names: Iterable[str]) -> bool:
    """Is this view one of the chosen ones?

    A view is chosen by its address. ``FIRST_VIEW`` stands for the dashboard's
    first view, whatever its address. A view without an address is never
    matched by name: an index would move as soon as other views are left out.
    """
    names = set(names)
    if index == 0 and FIRST_VIEW in names:
        return True
    path = view.get("path")
    return path is not None and str(path) != "" and str(path) in names


def publish_config(
    config: dict[str, Any],
    *,
    published: Iterable[str],
    support: Iterable[str] = (),
    dashboard: str | None = None,
    public_path: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """The dashboard as a visitor receives it.

    Only chosen views are kept; every other view is not even sent to the
    browser. *Published* views are the tabs (a view the owner hides from
    everyone stays without a tab). *Supporting* views are sent too,
    but hidden from the tab bar: they exist so that cards on a published view
    can find what they refer to (reference cards, popups that reuse a card
    from another tab). A supporting view is still readable by a visitor who
    looks for it, so it must hold nothing private either.

    If none of the chosen views exists any more (renamed, deleted), nothing is
    published rather than the first view: that one was never chosen.

    The dashboard's own address is replaced by the public one wherever it is
    written, so links between views keep working and the real name of the
    dashboard stays home.
    """
    published = set(published)
    support = set(support) - published
    views: list[dict[str, Any]] = []
    for index, view in enumerate(config.get("views") or []):
        if not isinstance(view, dict):
            continue
        if view_selected(view, index, published):
            if view.get("visible") is False or view.get("visible") == []:
                # Hidden from everyone by the owner: published, and still
                # reached only by a link, as on the owner's own dashboard.
                views.append({**view, "visible": False})
            else:
                # Otherwise ``visible`` lists the owner's users, and the
                # visitor is none of them: a published view is a tab.
                views.append({k: v for k, v in view.items() if k != "visible"})
        elif view_selected(view, index, support):
            views.append({**view, "visible": False})
    out: dict[str, Any] = {**config, "views": views}
    if title:
        out["title"] = title
    if dashboard and public_path:
        out = rewrite_paths(out, dashboard, public_path)
    return out


def has_published_view(config: dict[str, Any], published: Iterable[str]) -> bool:
    """Does the dashboard still have at least one of the views chosen to be public?"""
    published = set(published)
    return any(
        isinstance(view, dict) and view_selected(view, index, published)
        for index, view in enumerate(config.get("views") or [])
    )


def _path_pattern(dashboard: str) -> re.Pattern[str]:
    # "/dashboard-name" as a whole path segment at the start of a path: not
    # "/local/dashboard-name/x.png", not "/dashboard-name-2".
    return re.compile(
        r"(?<![^\s\"'`(=,>\[])/" + re.escape(dashboard) + r"(?=$|[/?#\"'`\s)<,\]])"
    )


def rewrite_text(text: str, dashboard: str, public_path: str) -> str:
    """One string with the dashboard's address replaced by the public one."""
    if text == dashboard:
        return public_path
    if dashboard not in text:
        return text
    return _path_pattern(dashboard).sub("/" + public_path, text)


def rewrite_paths(node: Any, dashboard: str, public_path: str) -> Any:
    """A copy of ``node`` with the dashboard's address replaced everywhere."""
    if isinstance(node, str):
        return rewrite_text(node, dashboard, public_path)
    if isinstance(node, dict):
        return {key: rewrite_paths(value, dashboard, public_path) for key, value in node.items()}
    if isinstance(node, list):
        return [rewrite_paths(item, dashboard, public_path) for item in node]
    return node


def collect_strings(node: Any) -> set[str]:
    """Every string value in a piece of dashboard configuration."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value:
                found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    return found


_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_STATISTIC_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+$")
_EMBEDDED_ENTITY_RE = re.compile(r"(?<![a-z0-9_.])([a-z][a-z_]*\.[a-z0-9][a-z0-9_]*)(?![a-z0-9_]*\()")


def referenced_ids(node: Any) -> tuple[set[str], set[str], set[str]]:
    """Entity ids and external statistic ids mentioned in dashboard configuration.

    Returns ``(entities, statistics, candidates)``.

    Card configs are free-form, especially custom cards, so this looks at every
    string value rather than known keys. ``entities`` are values that are an
    entity id and nothing else. ``candidates`` are things shaped like an entity
    id *inside* a longer string — a JavaScript or Jinja template such as
    ``states['sensor.pool'].state`` — and must be checked against the entities
    that really exist before they are trusted (``this.config`` has the same
    shape). Either way the allowlist only ever grows by something the owner
    wrote into a published view themselves.
    """
    entities: set[str] = set()
    statistics: set[str] = set()
    candidates: set[str] = set()
    for value in collect_strings(node):
        if _ENTITY_RE.match(value):
            entities.add(value)
        elif _STATISTIC_RE.match(value):
            statistics.add(value)
        elif "." in value:
            candidates.update(_EMBEDDED_ENTITY_RE.findall(value))
    return entities, statistics, candidates - entities


def pin_default_panel(data: Any, public_path: str) -> Any:
    """Point every ``default_panel`` in frontend user/system data at the public path.

    The owner's default dashboard (Settings -> Dashboards) reaches the visitor
    through the frontend's user and system data. For the visitor that panel does
    not exist, and the frontend stops on its loading screen trying to open it;
    its name is also nothing a visitor needs to learn.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "default_panel":
                data[key] = public_path
            else:
                pin_default_panel(value, public_path)
    elif isinstance(data, list):
        for item in data:
            pin_default_panel(item, public_path)
    return data


# What a visitor's frontend learns about the installation itself. The frontend
# needs the units, the language and the list of loaded components to draw
# cards; it does not need to know where the house is.
_CONFIG_BLANK: dict[str, Any] = {
    "latitude": 0,
    "longitude": 0,
    "elevation": 0,
    "radius": 0,
    "config_dir": "",
    "allowlist_external_dirs": [],
    "allowlist_external_urls": [],
    "internal_url": None,
    "external_url": None,
}


_LOCATION_KEYS = frozenset({"latitude", "longitude", "elevation", "radius"})


def public_core_config(
    config: dict[str, Any], location_name: str | None = None, hide_location: bool = True
) -> dict[str, Any]:
    """Home Assistant's core configuration without the home's coordinates,
    addresses and folders. The installation's name becomes the public title.

    The coordinates can be left in (``hide_location=False``) for an owner
    whose published views show the place anyway and have a card that centres
    on it, such as a weather radar."""
    out = dict(config)
    for key, value in config.items():
        if key in _LOCATION_KEYS and not hide_location:
            continue
        if key in _CONFIG_BLANK:
            out[key] = _CONFIG_BLANK[key]
        elif key.endswith(("_dir", "_dirs", "_url", "_urls")):
            # Folders and addresses under a name this version does not know yet.
            out[key] = [] if isinstance(value, list) else None
    if "location_name" in out:
        out["location_name"] = location_name or "Home"
    return out


# Device details a card may legitimately show.
_DEVICE_KEEP = (
    "id", "name", "name_by_user", "area_id", "manufacturer", "model", "model_id",
    "entry_type", "disabled_by", "labels", "sw_version", "hw_version",
    "created_at", "modified_at", "primary_config_entry",
)


def public_devices(devices: list[Any], allowed: set[str] | None) -> list[dict[str, Any]]:
    """Only the devices behind published entities, without their hardware
    addresses, serial numbers and configuration links."""
    out: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        if allowed is not None and device.get("id") not in allowed:
            continue
        slim = {key: device[key] for key in _DEVICE_KEEP if key in device}
        slim.update(
            connections=[], identifiers=[], config_entries=[], config_entries_subentries={},
            serial_number=None, configuration_url=None,
            via_device_id=device.get("via_device_id")
            if allowed is None or device.get("via_device_id") in allowed else None,
        )
        out.append(slim)
    return out
