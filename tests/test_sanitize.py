"""The two checks the mirror takes from sanitize.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "public_access"))

import sanitize  # noqa: E402


def test_a_numeric_view_path_still_matches():
    """`path: 123456` in YAML arrives as an int; the option is text."""
    assert sanitize.view_matches({"path": 123456}, "123456")
    assert sanitize.view_matches({"path": "solar"}, "solar")
    assert not sanitize.view_matches({"path": "solar"}, "costs")


def test_a_view_without_an_address_matches_only_when_none_was_chosen():
    assert not sanitize.view_matches({"title": "no path"}, "123456")
    assert sanitize.view_matches({"title": "no path"}, None)


def test_the_owner_default_panel_never_reaches_the_visitor():
    """HA 2026.8+: a system default dashboard made the mirror hang on "Loading"."""
    event = {"value": {"default_panel": "dashboard-tablet"}}
    assert sanitize.pin_default_panel(event, "public") == {"value": {"default_panel": "public"}}
    nested = {"core": {"default_panel": "dashboard-tablet"}, "sidebar": {"panelOrder": ["x"]}}
    sanitize.pin_default_panel(nested, "public")
    assert nested["core"]["default_panel"] == "public"
    assert nested["sidebar"] == {"panelOrder": ["x"]}
    assert sanitize.pin_default_panel(None, "public") is None


# -- several views ------------------------------------------------------------------

DASHBOARD = {
    "title": "Elm Street",
    "button_card_templates": {"big": {"entity": "sensor.shared"}},
    "views": [
        {"title": "Home", "cards": [{"type": "button", "tap_action": {"navigation_path": "/dashboard-elm/solar"}}]},
        {"title": "Solar", "path": "solar", "visible": [{"user": "abc"}], "cards": [{"entity": "sensor.pv"}]},
        {"title": "Sources", "path": "sources", "cards": [{"entity": "light.porch"}]},
        {"title": "Private", "path": "private", "cards": [{"entity": "lock.safe"}]},
    ],
}


def _titles(config):
    return [(v["title"], v.get("visible", True)) for v in config["views"]]


def test_an_entry_from_before_several_views_still_means_its_one_view():
    assert sanitize.configured_views({"view_path": "solar"}) == ["solar"]
    assert sanitize.configured_views({"view_path": None}) == [sanitize.FIRST_VIEW]
    assert sanitize.configured_views({}) == [sanitize.FIRST_VIEW]
    assert sanitize.configured_views({"views": ["solar", "costs"], "view_path": "solar"}) == ["solar", "costs"]
    assert sanitize.configured_views({}, "support_views") == []


def test_chosen_views_are_kept_in_dashboard_order_and_the_rest_are_not_sent():
    out = sanitize.publish_config(DASHBOARD, published=["solar", sanitize.FIRST_VIEW])
    assert _titles(out) == [("Home", True), ("Solar", True)]
    assert "lock.safe" not in str(out) and "light.porch" not in str(out)


def test_a_published_view_is_a_tab_whatever_users_it_was_limited_to():
    """`visible` lists the owner's users; the visitor is none of them."""
    out = sanitize.publish_config(DASHBOARD, published=["solar"])
    assert "visible" not in out["views"][0]


def test_a_view_the_owner_hides_from_everyone_keeps_no_tab_when_published():
    config = {"views": [{"path": "a"}, {"path": "b", "visible": False}, {"path": "c", "visible": []}]}
    out = sanitize.publish_config(config, published=["a", "b", "c"])
    assert [(v["path"], v.get("visible", True)) for v in out["views"]] == [("a", True), ("b", False), ("c", False)]


def test_a_supporting_view_is_sent_but_hidden_from_the_tab_bar():
    out = sanitize.publish_config(DASHBOARD, published=["solar"], support=["sources", "solar"])
    assert _titles(out) == [("Solar", True), ("Sources", False)]


def test_a_view_without_an_address_is_only_ever_the_first_view():
    config = {"views": [{"title": "A"}, {"title": "B"}, {"title": "C", "path": "c"}]}
    out = sanitize.publish_config(config, published=[sanitize.FIRST_VIEW, "c", "1", "None"])
    assert _titles(out) == [("A", True), ("C", True)]


def test_the_dashboards_own_address_and_name_stay_home():
    out = sanitize.publish_config(
        DASHBOARD, published=[sanitize.FIRST_VIEW], dashboard="dashboard-elm", public_path="solar", title="Solar")
    assert out["title"] == "Solar"
    assert out["views"][0]["cards"][0]["tap_action"]["navigation_path"] == "/solar/solar"
    assert "dashboard-elm" not in str(out) and "Elm Street" not in str(out)
    assert DASHBOARD["title"] == "Elm Street", "the stored dashboard is not touched"


def test_only_the_dashboard_address_is_rewritten():
    rewrite = lambda text: sanitize.rewrite_text(text, "dashboard-elm", "pub")
    assert rewrite("dashboard-elm") == "pub"
    assert rewrite("/dashboard-elm") == "/pub"
    assert rewrite("/dashboard-elm/solar?edit=1#x") == "/pub/solar?edit=1#x"
    assert rewrite("[go](/dashboard-elm/solar)") == "[go](/pub/solar)"
    assert rewrite("navigate('/dashboard-elm/a'); navigate(\"/dashboard-elm\")") == "navigate('/pub/a'); navigate(\"/pub\")"
    # Not the dashboard: a file under /local, a longer name, a plain word.
    assert rewrite("/local/dashboard-elm/plan.png") == "/local/dashboard-elm/plan.png"
    assert rewrite("/dashboard-elm-old/solar") == "/dashboard-elm-old/solar"
    assert rewrite("the dashboard-elm page") == "the dashboard-elm page"


def test_entities_are_found_as_values_and_inside_templates():
    entities, statistics, candidates = sanitize.referenced_ids({
        "entity": "sensor.pv",
        "stat": "energy:grid_import",
        "label": "[[[ return states['sensor.pool_temp'].state + hass.states[\"cover.gate\"].state ]]]",
        "content": "{{ states('sensor.outside') | float }} {{ this.config }}",
        "call": "console.log(some.thing)",
    })
    assert entities == {"sensor.pv"}
    assert statistics == {"energy:grid_import"}
    # Candidates are checked against the entities that exist before use.
    assert {"sensor.pool_temp", "cover.gate", "sensor.outside"} <= candidates
    assert "console.log" not in candidates


def test_shared_templates_at_the_top_of_the_dashboard_are_scanned_too():
    out = sanitize.publish_config(DASHBOARD, published=["solar"])
    entities, _, _ = sanitize.referenced_ids(out)
    assert entities == {"sensor.pv", "sensor.shared"}


def test_the_core_configuration_does_not_locate_the_home():
    public = sanitize.public_core_config({
        "latitude": 47.6, "longitude": -122.3, "elevation": 50, "radius": 100,
        "location_name": "Elm Street", "config_dir": "/config", "internal_url": "http://192.168.1.2:8123",
        "allowlist_external_dirs": ["/media"], "future_secret_dirs": ["/x"], "time_zone": "Europe/Rome",
        "components": ["energy"], "unit_system": {"length": "km"},
    }, "Solar")
    assert public == {
        "latitude": 0, "longitude": 0, "elevation": 0, "radius": 0,
        "location_name": "Solar", "config_dir": "", "internal_url": None,
        "allowlist_external_dirs": [], "future_secret_dirs": [], "time_zone": "Europe/Rome",
        "components": ["energy"], "unit_system": {"length": "km"},
    }
    assert sanitize.public_core_config({"location_name": "Elm Street"})["location_name"] == "Home"


def test_only_published_devices_and_never_their_hardware_addresses():
    devices = [
        {"id": "a", "name": "Inverter", "connections": [["mac", "aa:bb"]], "identifiers": [["solar", "SN1"]],
         "serial_number": "SN1", "configuration_url": "http://192.168.1.9", "via_device_id": "b", "model": "X"},
        {"id": "b", "name": "Gateway"},
    ]
    out = sanitize.public_devices(devices, {"a"})
    assert [d["id"] for d in out] == ["a"]
    assert out[0]["model"] == "X" and out[0]["connections"] == [] and out[0]["identifiers"] == []
    assert out[0]["serial_number"] is None and out[0]["configuration_url"] is None
    assert out[0]["via_device_id"] is None, "a device that is not published is not pointed at"


def test_the_coordinates_can_be_left_in_and_nothing_else():
    public = sanitize.public_core_config(
        {"latitude": 47.6, "longitude": -122.3, "elevation": 50, "radius": 100,
         "location_name": "Elm Street", "config_dir": "/config", "external_url": "https://x.example"},
        "Solar", hide_location=False)
    assert public == {"latitude": 47.6, "longitude": -122.3, "elevation": 50, "radius": 100,
                      "location_name": "Solar", "config_dir": "", "external_url": None}


def test_conditions_written_in_a_view_are_found_wherever_they_are():
    sun = {"condition": "sun", "after": "sunset"}
    state = {"condition": "state", "entity": "light.porch", "state": "on"}
    config = {"views": [{"cards": [
        {"type": "conditional", "conditions": [state], "card": {"type": "tile", "visibility": [
            {"condition": "or", "conditions": [sun]}]}},
        {"type": "entities", "entities": [{"entity": "light.porch", "condition": 5}]},
    ]}]}
    found = sanitize.collect_conditions(config)
    assert sanitize.condition_key({"after": "sunset", "condition": "sun"}) in found
    assert sanitize.condition_key(state) in found
    assert sanitize.condition_key({"condition": "or", "conditions": [sun]}) in found
    assert len(found) == 3


def test_camera_cards_reach_a_visitor_without_their_talk_button():
    card = {"type": "custom:advanced-camera-card", "cameras": [{"camera_entity": "camera.front"}],
            "menu": {"style": "overlay", "buttons": {"microphone": {"enabled": True, "type": "toggle"}, "mute": {"enabled": True}}},
            "live": {"auto_unmute": ["selected"], "microphone": {"always_connected": True, "disconnect_seconds": 90}}}
    bare = {"type": "custom:frigate-card", "cameras": []}
    config = {"views": [{"path": "cams", "cards": [
        {"type": "vertical-stack", "cards": [card, {"type": "custom:dashboard-card-reference", "card": bare}]},
        {"type": "markdown", "content": "hello", "menu": {"buttons": {"microphone": {"enabled": True}}}},
    ]}]}
    sent = sanitize.publish_config(config, published={"cams"})
    stack, markdown = sent["views"][0]["cards"]
    first, second = stack["cards"][0], stack["cards"][1]["card"]
    assert first["menu"] == {"style": "overlay", "buttons": {"microphone": {"enabled": False, "type": "toggle"}, "mute": {"enabled": True}}}
    assert first["live"] == {"auto_unmute": ["selected"], "microphone": {"always_connected": False, "disconnect_seconds": 90}}
    assert second["menu"]["buttons"]["microphone"] == {"enabled": False}
    assert second["live"]["microphone"] == {"always_connected": False}
    assert markdown["menu"]["buttons"]["microphone"] == {"enabled": True}, "only camera cards are touched"
    assert card["menu"]["buttons"]["microphone"]["enabled"] is True, "the owner's own configuration is not"
