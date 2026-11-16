"""Which entities an auto-entities card shows.

A card like ``custom:auto-entities`` names no entities: it has filters
("every sensor with device class battery", "everything from this
integration", a template) and picks the entities itself, in the browser, from
the states it has. A visitor's browser has only the states of published
entities, and what is published is found by reading the views for entity ids.
So such a card found nothing to pick from and stayed empty.

Here the card's filters are worked out on the server, the way the card works
them out (ported from auto-entities 1.16: ``filter.ts`` and ``match.ts``), and
the entities they select are published along with the ones written out.

What that means for the owner: an auto-entities card on a published view
publishes what its filters select, now and as that changes. ``sort.count``
and ``sort.first`` (show only the first few) are not applied: every entity
the filter selects is published, not only the ones the card has room for.
A rule this module does not know selects nothing, as in the card itself.

Pure: no Home Assistant imports. ``World`` is the data the card would have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any, Callable, Iterator

CARD_TYPES = frozenset({"custom:auto-entities"})
MAX_FILTERS = 200
_AGO = re.compile(r"([mhd])\s+ago\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
# Keys of a filter that say what to do with the entities, not which ones.
_NOT_RULES = frozenset({"type", "options", "sort"})


@dataclass
class World:
    """What an auto-entities card can see of a home."""

    # entity_id -> {"state", "attributes", "last_changed", "last_updated"}
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    # entity_id -> {"platform", "config_entry_id", "device_id", "area_id",
    #               "entity_category", "hidden_by", "labels"}
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    # device id -> {"name", "name_by_user", "manufacturer", "model", "area_id", "labels"}
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    areas: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {"name", "floor_id"}
    floors: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {"name", "level"}
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {"name"}
    now: datetime | None = None


def _number(value: Any) -> float | None:
    """JavaScript's parseFloat: the number a text starts with, or nothing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = _NUMBER.match(str(value)) if value is not None else None
    return float(found.group(0)) if found else None


def matcher(pattern: Any, now: datetime | None = None) -> Callable[[Any], bool]:
    """auto-entities' way of comparing a value with a pattern: equal, a glob
    (``*``), a regular expression (``/.../``), or a number comparison
    (``< 40``, ``>= 5``, ``! 3``), optionally on an age (``> 2 h ago``)."""
    tests: list[Callable[[Any], bool]] = []
    transforms: list[Callable[[Any], Any]] = []
    if isinstance(pattern, str):
        if pattern.startswith("$$"):
            pattern = pattern[2:]
            transforms.append(lambda value: json.dumps(value, separators=(",", ":"), default=str))
        if (pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 1) or "*" in pattern:
            source = pattern
            if not source.startswith("/"):
                source = "/^" + source.replace("*", ".*") + "$/"
            try:
                regex = re.compile(source[1:-1])
                tests.append(lambda value: isinstance(value, str) and regex.search(value) is not None)
            except re.error:
                pass
        aged = _AGO.search(pattern)
        if aged:
            pattern = pattern[: aged.start()]
            unit = aged.group(1).lower()

            def age(value: Any) -> Any:
                moment = value if isinstance(value, datetime) else _parse_time(value)
                if moment is None:
                    return None
                minutes = ((now or datetime.now(timezone.utc)) - moment).total_seconds() / 60
                return minutes / 60 if unit == "h" else minutes / 1440 if unit == "d" else minutes

            transforms.append(age)

        def compare(prefix: str, test: Callable[[float, float], bool]) -> None:
            if pattern.startswith(prefix):
                wanted = _number(pattern[len(prefix):])
                unequal = prefix in ("!=", "!")
                if wanted is None:
                    # parseFloat gives "not a number": nothing is less than,
                    # more than or equal to that, and everything differs.
                    if unequal:
                        tests.append(lambda value: True)
                    return
                tests.append(
                    lambda value: unequal if (got := _number(value)) is None else test(got, wanted)
                )

        compare("<=", lambda a, b: a <= b)
        compare(">=", lambda a, b: a >= b)
        compare("==", lambda a, b: a == b)
        compare("!=", lambda a, b: a != b)
        compare("<", lambda a, b: a < b)
        compare(">", lambda a, b: a > b)
        compare("!", lambda a, b: a != b)
        compare("=", lambda a, b: a == b)
    literal = pattern

    def equal(value: Any) -> bool:
        if isinstance(value, bool) or isinstance(literal, bool):
            return value is literal
        return value == literal

    tests.append(equal)

    def match(value: Any) -> bool:
        for transform in transforms:
            value = transform(value)
        if value is None and transforms:
            return False
        return any(test(value) for test in tests)

    return match


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _ago(value: Any) -> Any:
    return value if not isinstance(value, str) or _AGO.search(value) else value + "m ago"


def _rule(name: str, value: Any, world: World, budget: list[int]) -> Callable[[str], bool] | None:
    """One rule of a filter as a test of an entity id, None for a key that
    selects nothing by itself, and a refusal for a rule that is not known."""
    states, registry = world.states, world.entities
    match = matcher(value, world.now) if not isinstance(value, (dict, list)) else None
    never: Callable[[str], bool] = lambda entity_id: False  # noqa: E731

    def device_of(entity_id: str) -> dict[str, Any] | None:
        return world.devices.get((registry.get(entity_id) or {}).get("device_id") or "")

    def area_of(entity_id: str) -> dict[str, Any] | None:
        entry = registry.get(entity_id)
        if entry is None:
            return None
        area = world.areas.get(entry.get("area_id") or "")
        if area is None:
            device = device_of(entity_id)
            area = world.areas.get((device or {}).get("area_id") or "")
            if area is not None:
                return {**area, "area_id": (device or {}).get("area_id")}
            return None
        return {**area, "area_id": entry.get("area_id")}

    if name in _NOT_RULES:
        return None
    if name in ("not", "and", "or"):
        parts = [value] if name == "not" else value
        if not isinstance(parts, list) or not all(isinstance(part, dict) for part in parts):
            return never
        tests = [build_filter(part, world, budget) for part in parts]
        if name == "not":
            return lambda entity_id: not tests[0](entity_id)
        if name == "and":
            return lambda entity_id: all(test(entity_id) for test in tests)
        return lambda entity_id: any(test(entity_id) for test in tests)
    if name == "attributes":
        if not isinstance(value, dict):
            return never
        wanted = []
        for key, pattern in value.items():
            path = str(key).split(" ")[0].split(":")
            wanted.append((path, matcher(pattern, world.now)))

        def attributes(entity_id: str) -> bool:
            held = states[entity_id].get("attributes") or {}
            for path, test in wanted:
                found: Any = held
                for step in path:
                    found = found.get(step) if isinstance(found, dict) else None
                if not test(found):
                    return False
            return True

        return attributes
    if name == "group":
        return lambda entity_id: entity_id in (
            ((states.get(str(value)) or {}).get("attributes") or {}).get("entity_id") or []
        )
    if match is None:
        return never
    if name == "domain":
        return lambda entity_id: match(entity_id.split(".")[0])
    if name == "entity_id":
        return match
    if name == "state":
        return lambda entity_id: match(states[entity_id].get("state"))
    if name == "name":
        return lambda entity_id: match((states[entity_id].get("attributes") or {}).get("friendly_name"))
    if name in ("last_changed", "last_updated"):
        aged = matcher(_ago(value), world.now)
        return lambda entity_id: aged(states[entity_id].get(name))
    if name == "last_triggered":
        aged = matcher(_ago(value), world.now)
        return lambda entity_id: aged((states[entity_id].get("attributes") or {}).get("last_triggered"))
    if name == "integration":
        return lambda entity_id: (entry := registry.get(entity_id)) is not None and (
            match(entry.get("platform")) or match(entry.get("config_entry_id"))
        )
    if name in ("entity_category", "hidden_by"):
        return lambda entity_id: (entry := registry.get(entity_id)) is not None and match(entry.get(name))
    if name == "device":
        return lambda entity_id: (device := device_of(entity_id)) is not None and (
            match(device.get("id")) or match(device.get("name_by_user")) or match(device.get("name"))
        )
    if name in ("device_manufacturer", "device_model"):
        key = name.split("_", 1)[1]
        return lambda entity_id: (device := device_of(entity_id)) is not None and match(device.get(key))
    if name == "area":
        return lambda entity_id: (area := area_of(entity_id)) is not None and (
            match(area.get("name")) or match(area.get("area_id"))
        )
    if name in ("floor", "level"):

        def floor(entity_id: str) -> bool:
            area = area_of(entity_id)
            found = world.floors.get((area or {}).get("floor_id") or "")
            if found is None:
                return False
            if name == "level":
                return match(found.get("level"))
            return match(found.get("name")) or match((area or {}).get("floor_id"))

        return floor
    if name == "label":

        def named(label: Any) -> bool:
            return match(label) or match((world.labels.get(label) or {}).get("name"))

        def label(entity_id: str) -> bool:
            entry = registry.get(entity_id)
            if entry is None:
                return False
            if any(named(item) for item in entry.get("labels") or []):
                return True
            return any(named(item) for item in (device_of(entity_id) or {}).get("labels") or [])

        return label
    # A rule this module does not know (a newer auto-entities, a typing
    # slip): the card itself would select nothing with it.
    return never


def build_filter(filter_: Any, world: World, budget: list[int] | None = None) -> Callable[[str], bool]:
    """One include or exclude filter as a test of an entity id. Every rule of
    it must hold; a filter without rules selects nothing."""
    budget = budget if budget is not None else [MAX_FILTERS]
    budget[0] -= 1
    if budget[0] < 0 or not isinstance(filter_, dict):
        return lambda entity_id: False
    rules = []
    for key, value in filter_.items():
        rule = _rule(str(key).strip().split(" ")[0].strip(), value, world, budget)
        if rule is not None:
            rules.append(rule)
    if not rules:
        return lambda entity_id: False

    def test(entity_id: str) -> bool:
        if entity_id not in world.states:
            return False
        try:
            return all(rule(entity_id) for rule in rules)
        except Exception:  # noqa: BLE001 - an odd attribute must not sink the list
            return False

    return test


def template_entities(rendered: Any) -> set[str]:
    """The entities a rendered ``filter.template`` names: a list of ids, of
    rows with an ``entity``, or ids separated by commas or spaces."""
    if isinstance(rendered, str):
        rendered = [part for part in re.split(r"[\s,]+", rendered) if part]
    found: set[str] = set()
    if isinstance(rendered, (list, tuple)):
        for item in rendered:
            if isinstance(item, dict):
                item = item.get("entity")
            if isinstance(item, str) and "." in item:
                found.add(item.strip())
    return found


def cards_in(node: Any) -> Iterator[dict[str, Any]]:
    """Every auto-entities card in a piece of dashboard configuration,
    however deep (in a stack, in a fold, in a wrapper)."""
    stack = [node]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get("type") in CARD_TYPES and isinstance(value.get("filter"), dict):
                yield value
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def selected(
    card: dict[str, Any],
    world: World,
    render: Callable[[str, dict[str, Any]], Any] | None = None,
) -> set[str]:
    """The entities one auto-entities card selects from the home.

    ``render`` renders the card's ``filter.template`` (the owner's own
    template, with the card's configuration as ``config``); without it a
    template adds nothing."""
    filters = card.get("filter") or {}
    budget = [MAX_FILTERS]
    found: set[str] = set()
    template = filters.get("template")
    if isinstance(template, str) and render is not None:
        try:
            found |= {e for e in template_entities(render(template, {"config": card})) if e in world.states}
        except Exception:  # noqa: BLE001 - a template that fails adds nothing
            pass
    include = filters.get("include")
    for item in include if isinstance(include, list) else []:
        if not isinstance(item, dict) or item.get("type") is not None:
            continue  # a fixed row (a divider, a section), not a filter
        test = build_filter(item, world, budget)
        found |= {entity_id for entity_id in world.states if test(entity_id)}
    exclude = filters.get("exclude")
    for item in exclude if isinstance(exclude, list) else []:
        test = build_filter(item, world, budget)
        found = {entity_id for entity_id in found if not test(entity_id)}
    return found
