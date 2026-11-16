"""Which entities an auto-entities card selects, worked out the card's way."""

from datetime import datetime, timedelta, timezone
import importlib.util
import pathlib
import sys

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "public_access"
spec = importlib.util.spec_from_file_location("autoentities", PACKAGE / "autoentities.py")
ae = importlib.util.module_from_spec(spec)
sys.modules["autoentities"] = ae
spec.loader.exec_module(ae)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def world():
    def state(value, changed_minutes_ago=5, **attributes):
        return {"state": value, "attributes": attributes,
                "last_changed": NOW - timedelta(minutes=changed_minutes_ago),
                "last_updated": (NOW - timedelta(minutes=changed_minutes_ago)).isoformat()}

    return ae.World(
        now=NOW,
        states={
            "sensor.front_door_battery": state("87", device_class="battery", friendly_name="Front door battery", battery_type="CR2032"),
            "sensor.attic_leak_battery": state("35", 600, device_class="battery", friendly_name="Attic leak battery", battery_type="AA"),
            "sensor.safe_battery": state("60", device_class="battery", friendly_name="Safe battery"),
            "sensor.mailbox_battery": state("unavailable", device_class="battery", friendly_name="Mailbox battery"),
            "sensor.outside_temperature": state("12.5", device_class="temperature", friendly_name="Outside", nested={"deep": {"value": 3}}),
            "light.kitchen": state("on", friendly_name="Kitchen light", brightness=200),
            "light.safe_room": state("off", friendly_name="Safe room light"),
            "group.downstairs": state("on", entity_id=["light.kitchen", "sensor.outside_temperature"]),
            "lock.front": state("locked", friendly_name="Front lock"),
        },
        entities={
            "sensor.front_door_battery": {"platform": "zha", "config_entry_id": "c1", "device_id": "d1", "area_id": None, "entity_category": "diagnostic", "hidden_by": None, "labels": []},
            "sensor.attic_leak_battery": {"platform": "battery_notes", "config_entry_id": "c2", "device_id": "d2", "area_id": "attic", "entity_category": None, "hidden_by": None, "labels": ["l1"]},
            "sensor.safe_battery": {"platform": "zha", "config_entry_id": "c1", "device_id": "d3", "area_id": None, "entity_category": None, "hidden_by": "user", "labels": []},
            "light.kitchen": {"platform": "hue", "config_entry_id": "c3", "device_id": None, "area_id": "kitchen", "entity_category": None, "hidden_by": None, "labels": []},
            "lock.front": {"platform": "zwave_js", "config_entry_id": "c4", "device_id": "d1", "area_id": None, "entity_category": None, "hidden_by": None, "labels": []},
        },
        devices={
            "d1": {"id": "d1", "name": "Front door sensor", "name_by_user": "Front door", "manufacturer": "Aqara", "model": "MCCGQ11LM", "area_id": "hall", "labels": []},
            "d2": {"id": "d2", "name": "Leak sensor", "name_by_user": None, "manufacturer": "FortrezZ", "model": "WWA", "area_id": "attic", "labels": []},
            "d3": {"id": "d3", "name": "Safe", "name_by_user": None, "manufacturer": "Acme", "model": "S1", "area_id": None, "labels": ["l2"]},
        },
        areas={"hall": {"name": "Hall", "floor_id": "ground"}, "attic": {"name": "Attic", "floor_id": "top"}, "kitchen": {"name": "Kitchen", "floor_id": "ground"}},
        floors={"ground": {"name": "Ground floor", "level": 0}, "top": {"name": "Top", "level": 2}},
        labels={"l1": {"name": "Leaks"}, "l2": {"name": "Valuables"}},
    )


def pick(*include, exclude=(), template=None, render=None):
    card = {"type": "custom:auto-entities", "filter": {"include": list(include), "exclude": list(exclude)}}
    if template is not None:
        card["filter"]["template"] = template
    return sorted(ae.selected(card, world(), render))


BATTERIES = ["sensor.attic_leak_battery", "sensor.front_door_battery", "sensor.mailbox_battery", "sensor.safe_battery"]


@pytest.mark.parametrize("pattern, value, expected", [
    ("on", "on", True), ("on", "off", False), ("on", "ON", False),
    ("sensor.*", "sensor.x", True), ("sensor.*", "binary_sensor.x", False), ("*battery*", "sensor.a_battery_level", True),
    ("/^light\\./", "light.kitchen", True), ("/kitchen$/", "light.kitchen", True), ("/kitchen$/", "light.kitchens", False),
    ("< 40", "35", True), ("< 40", "40", False), ("<= 40", "40", True), ("> 40", "87.5", True), (">= 87.5", "87.5", True),
    ("< 40", "unavailable", False), ("> 40", "unavailable", False), ("= 5", "5.0", True), ("== 5", "6", False),
    ("! 5", "6", True), ("! 5", "5", False), ("! 5", "unknown", True), ("!= 5", "5", True),  # the card's "!=" never excludes
    ("< 40", 35, True), (5, 5, True), (5, "5", False), (True, True, True), (True, 1, False), (None, None, True),
    ("$$*\"deep\"*", {"deep": 1}, True), ("/[/", "x", False),
])
def test_values_are_compared_the_cards_way(pattern, value, expected):
    assert ae.matcher(pattern, NOW)(value) is expected


def test_an_age_is_compared_in_minutes_hours_or_days():
    ten_hours = NOW - timedelta(hours=10)
    assert ae.matcher("> 9 h ago", NOW)(ten_hours) and not ae.matcher("> 11 h ago", NOW)(ten_hours.isoformat())
    assert ae.matcher("< 1 d ago", NOW)(ten_hours) and ae.matcher("> 500 m ago", NOW)(ten_hours)
    assert not ae.matcher("> 1 m ago", NOW)("not a time") and not ae.matcher("> 1 m ago", NOW)(None)


def test_filters_select_by_what_an_entity_is():
    assert pick({"attributes": {"device_class": "battery"}}) == BATTERIES
    assert pick({"domain": "light"}) == ["light.kitchen", "light.safe_room"]
    assert pick({"entity_id": "sensor.*_battery", "state": "<= 40"}) == ["sensor.attic_leak_battery"]
    assert pick({"name": "/[Ss]afe/"}) == ["light.safe_room", "sensor.safe_battery"]
    assert pick({"group": "group.downstairs"}) == ["light.kitchen", "sensor.outside_temperature"]
    assert pick({"attributes": {"nested:deep:value": "> 2"}}) == ["sensor.outside_temperature"]
    assert pick({"attributes": {"brightness": "> 100"}}) == ["light.kitchen"]
    assert pick({"attributes": {"device_class 1": "battery", "battery_type 2": "A*"}}) == ["sensor.attic_leak_battery"]


def test_filters_select_by_where_an_entity_belongs():
    assert pick({"integration": "battery_notes"}) == ["sensor.attic_leak_battery"]
    assert pick({"integration": "c1"}) == ["sensor.front_door_battery", "sensor.safe_battery"]
    assert pick({"device": "Front door"}) == ["lock.front", "sensor.front_door_battery"]
    assert pick({"device": "Leak*"}) == ["sensor.attic_leak_battery"]
    assert pick({"device_manufacturer": "Aqara", "domain": "sensor"}) == ["sensor.front_door_battery"]
    assert pick({"device_model": "WWA"}) == ["sensor.attic_leak_battery"]
    assert pick({"area": "Attic"}) == ["sensor.attic_leak_battery"]
    assert pick({"area": "hall"}) == ["lock.front", "sensor.front_door_battery"], "through the device"
    assert pick({"floor": "Ground floor"}) == ["light.kitchen", "lock.front", "sensor.front_door_battery"]
    assert pick({"level": 2}) == ["sensor.attic_leak_battery"]
    assert pick({"label": "Leaks"}) == ["sensor.attic_leak_battery"] and pick({"label": "l2"}) == ["sensor.safe_battery"]
    assert pick({"entity_category": "diagnostic"}) == ["sensor.front_door_battery"]
    assert pick({"hidden_by": "user"}) == ["sensor.safe_battery"]


def test_rules_combine_and_exclude():
    battery = {"attributes": {"device_class": "battery"}}
    assert pick(battery, exclude=[{"entity_id": "*safe*"}, {"state": "unavailable"}]) == ["sensor.attic_leak_battery", "sensor.front_door_battery"]
    assert pick({**battery, "not": {"state": "/^[0-9.]+$/"}}) == ["sensor.mailbox_battery"]
    assert pick({"or": [{"domain": "lock"}, {"state": "< 40", **battery}]}) == ["lock.front", "sensor.attic_leak_battery"]
    assert pick({"and": [battery, {"integration": "zha"}]}) == ["sensor.front_door_battery", "sensor.safe_battery"]
    assert pick({**battery, "last_changed": "> 60"}) == ["sensor.attic_leak_battery"]
    assert pick({**battery, "last_updated": "< 1 h ago", "state": "> 80"}) == ["sensor.front_door_battery"]
    assert pick({"domain": "light", "options": {"secondary_info": "last-changed"}, "sort": {"method": "state"}}) == ["light.kitchen", "light.safe_room"]


def test_what_is_not_understood_selects_nothing():
    """As in the card: an unknown rule is never true, a filter without rules
    selects nothing, and a fixed row is not a filter."""
    assert pick({"domain": "light", "a_rule_from_the_future": "x"}) == []
    assert pick({"options": {"x": 1}}) == [] and pick({}) == [] and pick("light.kitchen") == [] and pick(None) == []
    assert pick({"type": "section", "label": "Lights"}, {"domain": "lock"}) == ["lock.front"]
    assert pick({"not": "light.kitchen"}) == [] and pick({"or": {"domain": "light"}}) == []
    assert pick({"attributes": "battery"}) == [] and pick({"domain": ["light"]}) == []
    assert pick({"state": "on"}) == ["group.downstairs", "light.kitchen"], "only a state: that is what the card shows too"
    deep = {"domain": "light"}
    for _ in range(300):
        deep = {"not": {"not": deep}}
    assert pick(deep) == [], "and no filter is followed without end"


def test_a_template_names_entities_in_any_of_the_cards_forms():
    assert ae.template_entities("light.kitchen, lock.front\nsensor.gone") == {"light.kitchen", "lock.front", "sensor.gone"}
    assert ae.template_entities(["light.kitchen", {"entity": "lock.front", "name": "x"}, {"type": "divider"}, 5, None]) == {"light.kitchen", "lock.front"}
    assert ae.template_entities(None) == set() and ae.template_entities(7) == set() and ae.template_entities("none") == set()
    seen = []

    def render(template, variables):
        seen.append((template, variables["config"]["type"]))
        return ["light.kitchen", "sensor.not_there", {"entity": "lock.front"}]

    assert pick(template="{{ x }}", render=render, exclude=[{"domain": "lock"}]) == ["light.kitchen"]
    assert seen == [("{{ x }}", "custom:auto-entities")]
    assert pick(template="{{ x }}") == [], "not rendered: nothing"
    assert pick({"domain": "lock"}, template="{{ x }}", render=lambda t, v: 1 / 0) == ["lock.front"], "a failing template adds nothing"


def test_the_cards_are_found_wherever_they_are():
    inner = {"type": "custom:auto-entities", "card": {"type": "custom:fold-entity-row"}, "filter": {"include": [{"domain": "light"}]}}
    config = {"views": [{"cards": [
        {"type": "vertical-stack", "cards": [{"type": "entities", "entities": [inner, "light.kitchen"]}]},
        {"type": "custom:dashboard-card-reference", "card": {"type": "custom:auto-entities", "filter": {"template": "{{ x }}"}}},
        {"type": "custom:auto-entities", "entities": ["light.kitchen"]},       # no filter: nothing to work out
        {"type": "markdown", "content": "custom:auto-entities", "filter": {"include": []}},
    ]}]}
    found = list(ae.cards_in(config))
    assert len(found) == 2 and inner in found
