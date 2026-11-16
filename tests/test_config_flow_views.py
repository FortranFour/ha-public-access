"""The view step offers the dashboard's views by name."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.public_access.config_flow import (  # noqa: E402
    FIRST_VIEW,
    _stored_view,
    _stored_views,
    _with_missing,
    view_options,
)


def test_views_are_offered_by_name_with_their_address():
    dashboard = {"views": [
        {"title": "Home"},                      # first, no address: reachable as first view
        {"title": "Solar", "path": "solar"},
        {"title": "Numbers", "path": 123456},   # YAML number
        {"title": "No address"},                # cannot be picked
    ]}
    options = view_options(dashboard)
    assert [o["value"] for o in options] == [FIRST_VIEW, "solar", "123456"]
    assert options[0]["label"] == "Home — first view"
    assert options[2]["label"] == "Numbers — /123456"


def test_first_view_is_stored_as_no_path():
    assert _stored_view(FIRST_VIEW) is None
    assert _stored_view("") is None
    assert _stored_view("solar") == "solar"


def test_several_views_are_stored_by_name_without_blanks_or_repeats():
    assert _stored_views(["solar", " costs ", "", "solar", "/typed/", None]) == ["solar", "costs", "typed"]
    assert _stored_views(FIRST_VIEW) == [FIRST_VIEW]
    assert _stored_views([]) == []


def test_a_chosen_view_the_dashboard_lost_stays_listed():
    """Opening the options must not silently drop it."""
    listed = _with_missing(view_options({"views": [{"title": "Solar", "path": "solar"}]}), ["solar", "gone"])
    assert [(o["value"], o["label"]) for o in listed] == [("solar", "Solar — /solar"), ("gone", "/gone (not found)")]


# -- links ---------------------------------------------------------------------

from custom_components.public_access.config_flow import (  # noqa: E402
    PATH_PATTERN,
    entry_title,
    random_path,
    stored_more,
)


def test_a_further_link_is_offered_an_address_nobody_can_guess():
    seen = {random_path() for _ in range(200)}
    assert len(seen) == 200
    assert all(PATH_PATTERN.match(p) and len(p) == 12 and "_" not in p for p in seen)


def test_an_entry_is_named_after_who_the_link_is_for():
    assert entry_title("Builder", "k3x9") == "Builder (/k3x9)"
    assert entry_title("  ", "k3x9") == "/k3x9" and entry_title(None, "k3x9") == "/k3x9"


def test_further_dashboards_are_stored_with_addresses_of_their_own():
    boards = [
        {"url_path": "dashboard-guest", "title": "Guest Suite", "views": []},
        {"url_path": "dashboard-energy", "title": "Energy", "views": []},
        {"url_path": "dashboard-guest-2", "title": "Guest suite", "views": []},
    ]
    more = stored_more(
        boards, ["dashboard-guest", "dashboard-energy", "dashboard-guest-2", "dashboard-gone"],
        {"dashboard-guest": ["welcome"]}, "home", energy=True, frigate=True,
    )
    assert [(m["dashboard"], m["path"], m["views"]) for m in more] == [
        ("dashboard-guest", "home_guest_suite", ["welcome"]),
        ("dashboard-energy", "home_energy_2", []),      # "home_energy" is the Energy dashboard's
        ("dashboard-guest-2", "home_guest_suite_2", []),
        ("dashboard-gone", "home_dashboard_gone", []),
    ]
    assert all(PATH_PATTERN.match(m["path"]) for m in more)
