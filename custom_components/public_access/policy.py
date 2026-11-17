"""What a visitor may fetch besides entity states: pictures, video, recordings.

A dashboard with cameras needs more than states. Cards ask Home Assistant to
*sign* a path (a short-lived link to one picture or clip), fetch pictures
through ``/api/...``, or browse a Frigate camera's events. Each of those is a
way to read something, so each is checked here against what the owner
published, before anything is signed or forwarded:

* a camera or image entity must be on a published view;
* a Frigate camera must be published — its camera entity is on a published
  view, its name is written in a card there, or the owner listed it in the
  options;
* a Frigate event's picture or clip is served only if that event was first
  listed to a visitor through the (camera-filtered) event list.

Everything else is refused. Nothing here imports Home Assistant, so it can be
read and tested on its own.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit

# A path a visitor may name: plain characters only. No "..", no encoded
# surprises, nothing that could mean one thing to this check and another to
# the view that finally serves it.
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9_.\-/:@,+]*$")

# Signed links are for showing a picture or starting a clip, not for keeping.
MAX_SIGN_SECONDS = 3600

MAX_REMEMBERED_EVENTS = 20000

# A template that is not written in a published view word for word (a card
# assembled it: macros put in front, a reference filled in) is rendered only
# under watch. It gets this long to render...
CHECKED_TEMPLATE_SECONDS = 5
MAX_CHECKED_TEMPLATE_LENGTH = 32768
# ... and it is read before it is run. A template can do far more than read
# an entity's state: list entities and devices by area or integration, read
# device and config entry details, measure distances from the home. So an
# assembled template may use only what is listed here, which either reads
# nothing or reads the state of one entity it names outright, and that entity
# has to be published. Everything else, anything Home Assistant adds later
# included, is for templates the owner wrote in a published view.
MAX_TEMPLATE_NODES = 20000
# These read one entity, named by their first argument.
_READERS = frozenset(
    {
        "states", "state_attr", "is_state", "is_state_attr", "has_value",
        "state_translated", "state_attr_translated",
    }
)
# These read nothing: time, numbers, text.
_PURE_FUNCTIONS = frozenset(
    {
        "now", "utcnow", "today_at", "as_timestamp", "as_datetime", "as_local",
        "as_timedelta", "strptime", "timedelta", "relative_time", "time_since",
        "time_until", "float", "int", "bool", "is_number", "iif", "max", "min",
        "range", "namespace", "dict", "zip", "typeof", "log", "sin", "cos", "tan",
        "asin", "acos", "atan", "atan2", "sqrt", "average", "median",
        "statistical_mode", "clamp", "wrap", "remap", "slugify", "ordinal",
        "bitwise_and", "bitwise_or", "bitwise_xor", "md5", "sha1", "sha256", "sha512",
        "set", "tuple", "combine", "flatten", "intersect", "union", "difference",
        "symmetric_difference", "urlencode",
    }
)
# Plain numbers, and a loop's own counter. A template may call its own
# variables "e" or "pi": a number is all it could reach by that name.
_PURE_VALUES = frozenset({"loop", "pi", "e", "tau"})
_PURE_FILTERS = frozenset(
    {
        # Jinja's own
        "abs", "batch", "capitalize", "center", "count", "d", "default", "dictsort",
        "e", "escape", "filesizeformat", "first", "float", "forceescape", "format",
        "indent", "int", "items", "join", "last", "length", "list", "lower", "max",
        "min", "random", "replace", "reverse", "round", "safe", "slice", "sort",
        "string", "striptags", "sum", "title", "tojson", "trim", "truncate",
        "unique", "upper", "urlencode", "wordcount", "wordwrap",
        # Home Assistant's that work on the value they are given and nothing else
        "multiply", "add", "log", "sin", "cos", "tan", "asin", "acos", "atan",
        "atan2", "sqrt", "as_timestamp", "as_datetime", "as_timedelta", "as_local",
        "timestamp_custom", "timestamp_local", "timestamp_utc", "to_json",
        "from_json", "is_defined", "average", "median", "statistical_mode",
        "regex_match", "regex_search", "regex_replace", "regex_findall",
        "regex_findall_index", "bitwise_and", "bitwise_or", "bitwise_xor", "ord",
        "base64_encode", "base64_decode", "slugify", "iif", "bool", "is_number",
        "ordinal", "relative_time", "time_since", "time_until", "contains",
        "flatten", "intersect", "difference", "union", "symmetric_difference",
        "md5", "sha1", "sha256", "sha512", "typeof", "clamp", "wrap", "remap",
        # these take the name of a filter or a test, which is checked as well
        "map", "select", "reject", "selectattr", "rejectattr",
    }
)
_PURE_TESTS = frozenset(
    {
        "defined", "undefined", "none", "number", "string", "mapping", "iterable",
        "sequence", "boolean", "integer", "float", "true", "false", "odd", "even",
        "divisibleby", "eq", "equalto", "==", "ne", "!=", "lt", "lessthan", "<",
        "le", "<=", "gt", "greaterthan", ">", "ge", ">=", "in", "sameas", "lower",
        "upper", "match", "search", "contains", "is_number", "list", "set",
        "tuple", "datetime", "string_like",
    }
)
# Where such a filter is given the name: (which names, at which argument).
_NAMED = {
    "map": (_PURE_FILTERS, 0),
    "select": (_PURE_TESTS, 0),
    "reject": (_PURE_TESTS, 0),
    "selectattr": (_PURE_TESTS, 1),
    "rejectattr": (_PURE_TESTS, 1),
}
# The parts a template may be made of: text, output, if, for, set, values,
# arithmetic and comparisons. No macros, imports or includes.
_TEMPLATE_PARTS = frozenset(
    {
        "Template", "Output", "TemplateData", "For", "If", "Assign", "AssignBlock",
        "Name", "NSRef", "Const", "Tuple", "List", "Dict", "Pair", "Keyword",
        "CondExpr", "Filter", "Test", "Call", "Getitem", "Getattr", "Slice",
        "Concat", "Compare", "Operand", "Mul", "Div", "FloorDiv", "Add", "Sub",
        "Mod", "Pow", "And", "Or", "Not", "Neg", "Pos", "Continue", "Break",
    }
)
_plain_reader: Any = None


def _read_template(text: str) -> tuple[Any, frozenset[str]]:
    """Jinja's reading of a template, with the two extensions Home Assistant
    switches on, and the names Jinja itself gives a meaning. Used where Home
    Assistant's own environment is not at hand."""
    global _plain_reader  # noqa: PLW0603
    if _plain_reader is None:
        from jinja2.sandbox import ImmutableSandboxedEnvironment

        _plain_reader = ImmutableSandboxedEnvironment(
            extensions=["jinja2.ext.loopcontrols", "jinja2.ext.do"]
        )
    return _plain_reader.parse(text), frozenset(_plain_reader.globals)


def _kind(node: Any) -> str:
    return type(node).__name__


def _known_value(
    node: Any, given: dict[str, Any], assigned: set[str], fixed: dict[str, Any] | None = None
) -> Any:
    """The value of an expression that is fixed before the template runs: a
    literal, a place in the variables sent along with it, or a name the
    template sets once, at its top, to a literal (``fixed``). LookupError
    for anything worked out while it runs."""
    kind = _kind(node)
    if kind == "Const":
        return node.value
    if kind == "Name" and node.ctx == "load" and fixed and node.name in fixed:
        return fixed[node.name]
    if kind == "Name" and node.ctx == "load" and node.name in given and node.name not in assigned:
        return given[node.name]
    if kind == "Getattr":
        base = _known_value(node.node, given, assigned, fixed)
        # "config.items" is the dictionary's method, not its "items" entry.
        if isinstance(base, dict) and node.attr in base and not hasattr(dict, node.attr):
            return base[node.attr]
    elif kind == "Getitem" and _kind(node.arg) == "Const":
        base = _known_value(node.node, given, assigned, fixed)
        key = node.arg.value
        if isinstance(base, dict) and isinstance(key, str) and key in base:
            return base[key]
        if isinstance(base, list) and isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(base):
            return base[key]
    raise LookupError


def _state_object_entity(node: Any, parents: dict[int, Any]) -> str | None:
    """The entity ``states.sensor.pv`` or ``states['sensor.pv']`` stands for,
    given the ``states`` in it; None if ``states`` is used any other way
    (``states.sensor`` is every sensor, ``states`` alone every entity)."""
    parent = parents.get(id(node))
    if _kind(parent) == "Getitem" and parent.node is node:
        key = parent.arg.value if _kind(parent.arg) == "Const" else None
        return key if isinstance(key, str) and "." in key else None
    if _kind(parent) == "Getattr" and parent.node is node:
        above = parents.get(id(parent))
        if _kind(above) == "Getattr" and above.node is parent:
            return f"{parent.attr}.{above.attr}"
    return None


def assembled_template_problem(
    template: str,
    entities: Iterable[str],
    variables: Any = None,
    read: Callable[[str], tuple[Any, Iterable[str]]] | None = None,
) -> str | None:
    """Why a template some card assembled may not be rendered for a visitor,
    or None if it may.

    It may if it is made of plain parts only (``_TEMPLATE_PARTS``), calls only
    functions, filters and tests that read nothing, and reads entities only
    through ``states('entity.id')`` and its like (or ``states.sensor.pv``)
    with the entity named outright, as text or as a variable sent along, and
    published. Such a
    template cannot read what is not published, however it is put together,
    so nothing is learnt from running it, from how long it takes or from how
    it fails.

    ``read`` turns the text into Jinja's tree and says which names the
    environment gives a meaning (Home Assistant's own does, in data.py, so
    that what is checked here is what will run there).
    """
    entities = entities if isinstance(entities, (set, frozenset)) else set(entities)
    given = variables if isinstance(variables, dict) else {}
    try:
        tree, meant = (read or _read_template)(template.strip())
        reserved = set(meant) | _READERS | _PURE_FUNCTIONS | _PURE_VALUES
    except Exception:  # noqa: BLE001 - not a template, or no parser to read it with
        return "it cannot be read as a template"
    nodes = []
    parents: dict[int, Any] = {}
    stack = [tree]
    while stack:
        node = stack.pop()
        nodes.append(node)
        if len(nodes) > MAX_TEMPLATE_NODES:
            return "it is too large"
        for child in node.iter_child_nodes():
            parents[id(child)] = node
            stack.append(child)
    assigned = {
        node.name for node in nodes if _kind(node) == "Name" and node.ctx in ("store", "param")
    }
    # A name set inside a loop means the template's own value there and Home
    # Assistant's function everywhere else. No name gets two meanings.
    twice = sorted((assigned & reserved) - _PURE_VALUES)
    if twice:
        return f"it gives {twice[0]} another meaning"
    for node in nodes:
        if _kind(node) not in _TEMPLATE_PARTS:
            return f"it uses a part ({_kind(node).lower()}) only a template written in a published view may"
    # "{% set w = 'weather.home' %}" at the top of the template, and w set
    # nowhere else: w is that text wherever it is used. (Used before the
    # line, it is nothing at all, which reads nothing.)
    stores: dict[str, int] = {}
    for node in nodes:
        if _kind(node) == "Name" and node.ctx in ("store", "param"):
            stores[node.name] = stores.get(node.name, 0) + 1
    fixed = {
        part.target.name: part.node.value
        for part in getattr(tree, "body", [])
        if _kind(part) == "Assign" and _kind(part.target) == "Name" and _kind(part.node) == "Const"
        and stores.get(part.target.name) == 1 and part.target.name not in given
    }
    called: dict[int, Any] = {}
    for node in nodes:
        kind = _kind(node)
        if kind in ("Call", "Filter", "Test") and (
            node.dyn_args is not None or node.dyn_kwargs is not None
        ):
            return "it passes arguments it works out as it runs"
        if kind == "Call":
            target = _kind(node.node)
            if target == "Name":
                if node.node.name in assigned:
                    return "it calls what it works out as it runs"
                called[id(node.node)] = node
            elif target != "Getattr":
                return "it calls what it works out as it runs"
        elif kind == "Filter":
            if node.name not in _PURE_FILTERS:
                return f"it uses the filter {node.name}"
            if node.name in _NAMED:
                names, at = _NAMED[node.name]
                if len(node.args) > at:
                    named = node.args[at]
                    if _kind(named) != "Const" or named.value not in names:
                        return f"it uses {node.name} with something other than a plain filter or test"
        elif kind == "Test":
            if node.name not in _PURE_TESTS:
                return f"it uses the test {node.name}"
        elif kind == "Getattr":
            if node.attr.startswith("_"):
                return "it reaches into an object"
        elif kind == "Getitem":
            if _kind(node.arg) == "Const" and isinstance(node.arg.value, str) and node.arg.value.startswith("_"):
                return "it reaches into an object"
        elif kind == "NSRef":
            if node.name not in assigned or node.attr.startswith("_"):
                return f"it uses {node.name}"
        elif kind == "Name" and node.ctx == "load":
            name = node.name
            if name in assigned:
                continue  # the template's own value
            if name in given:
                continue  # a value sent along: data, whatever it is called
            call = called.get(id(node))
            if name == "states" and call is None:
                # The other way to one entity: states.sensor.pv, states['sensor.pv'].
                entity = _state_object_entity(node, parents)
                if entity is None:
                    return "it uses states other than for one entity it names"
                if entity not in entities:
                    return f"it reads {entity[:100]}, not on a published view"
            elif name in _READERS:
                if call is None:
                    return f"it uses {name} other than by calling it"
                if not call.args:
                    return f"it calls {name} without naming an entity"
                try:
                    entity = _known_value(call.args[0], given, assigned, fixed)
                except LookupError:
                    return f"it works out as it runs which entity {name} reads"
                if not isinstance(entity, str) or entity not in entities:
                    return f"it reads {str(entity)[:100]}, not on a published view"
            elif name in _PURE_FUNCTIONS:
                if call is None:
                    return f"it uses {name} other than by calling it"
            elif name not in _PURE_VALUES:
                return f"it uses {name}"
    return None


# Conditions. Home Assistant decides for the page whether a conditional card
# or a card with a ``visibility`` shows: the page sends the condition and is
# told yes or no ("subscribe_condition"). A visitor's page could send any
# condition, and a yes or no about an entity is a reading of it, so a
# condition is passed on only if it is one of these plain kinds about
# published entities, or the owner wrote it in a published view as it is.
MAX_CONDITION_PARTS = 200
_CONDITION_GROUPS = frozenset({"and", "or", "not"})
_CONDITION_KEYS = {
    "state": frozenset(
        {"condition", "entity_id", "state", "attribute", "for", "match", "alias", "enabled"}
    ),
    "numeric_state": frozenset(
        {"condition", "entity_id", "above", "below", "attribute", "alias", "enabled"}
    ),
}
_TEMPLATE_MARK = re.compile(r"\{[{%#]")
# What the frontend itself writes for "this entity has a number": a numeric
# condition with neither bound.
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
_IS_NUMBER = re.compile(
    rf"^\{{\{{ is_number\((?:states\(({_JSON_STRING})\)"
    rf"|state_attr\(({_JSON_STRING}), {_JSON_STRING}\))\) \}}\}}$"
)

# How UIX puts a template together in the browser: the macros it needs, the
# values ("billets") it uses, then the template itself. Each piece is checked
# against what the owner wrote; see composed_of_written.
_UIX_MACRO = re.compile(r"\{% macro (\w+)\(([^)%{}]*)\) %\}")
_UIX_END_MACRO = "{% endmacro %}"
_UIX_AS_FUNCTION = re.compile(r"\n\{% set (\w+) = macro_(\w+) \| as_function %\}")
_UIX_IMPORT = re.compile(r"\{% from '([^'{}%]*)' import \w+ %\}")
_UIX_BILLET = re.compile(r"\{%- set \w+ = ")
_UIX_BILLET_END = " -%}"
# A value and nothing else: text in quotes, numbers, true/false/none, lists
# and dictionaries of those. No name, no call, nothing that reads anything.
_LITERAL = re.compile(
    r"""(?:\s|[\[\]{}(),:]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|none|True|False|None)\b"""
    r"""|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')*"""
)
_PARAM = re.compile(r"\w+(?: = .+)?")


def _is_literal(text: str) -> bool:
    return "{{" not in text and "{%" not in text and _LITERAL.fullmatch(text) is not None


def composed_of_written(template: str, written: set[str]) -> bool:
    """Is this template what UIX makes of pieces the owner wrote?

    UIX Forge does not send a card's template as it is written. It puts in
    front of it the macros the template uses (``{% macro name(a) %}...{%
    endmacro %}``) and the values it uses (``{%- set name = "value" -%}``).
    The whole is the owner's if every part is: each macro's body and the
    template at the end must be text the owner wrote, and everything in
    between must be exactly the wrapping UIX generates, around plain values.
    Anything else in there — one extra expression — and this says no.
    """
    rest = template
    for _ in range(200):  # more macros and values than any card has
        macro = _UIX_MACRO.match(rest)
        if macro:
            for param in (p.strip() for p in macro.group(2).split(",") if p.strip()):
                if not _PARAM.fullmatch(param):
                    return False
                if " = " in param and not _is_literal(param.split(" = ", 1)[1]):
                    return False
            body_start = macro.end()
            end = rest.find(_UIX_END_MACRO, body_start)
            while end >= 0 and rest[body_start:end] not in written:
                end = rest.find(_UIX_END_MACRO, end + 1)
            if end < 0:
                return False
            rest = rest[end + len(_UIX_END_MACRO):]
            wrapped = _UIX_AS_FUNCTION.match(rest)
            if wrapped and wrapped.group(1) == wrapped.group(2) and macro.group(1) == "macro_" + wrapped.group(1):
                rest = rest[wrapped.end():]
        elif (imported := _UIX_IMPORT.match(rest)) and imported.group(1) in written:
            rest = rest[imported.end():]
        elif billet := _UIX_BILLET.match(rest):
            end = rest.find(_UIX_BILLET_END, billet.end())
            while end >= 0 and not _is_literal(rest[billet.end():end]):
                end = rest.find(_UIX_BILLET_END, end + 1)
            if end < 0:
                return False
            rest = rest[end + len(_UIX_BILLET_END):]
        else:
            break
        if not rest.startswith("\n"):
            return False
        rest = rest[1:]
    return rest != template and rest in written


# Dashboard References (custom:dashboard-entity-reference) lets the owner
# write "[[entity:name]]" in a card and puts the entity it stands for in its
# place in the browser, in templates too. Such a template reaches Home
# Assistant as the owner's text with entity ids where the tokens were.
_REFERENCE_TOKEN = re.compile(r"\[\[entity:([A-Za-z0-9_.-]+)\]\]")
_REFERENCE_FILL = r"[a-z_]+\.[a-z0-9_]+"


def token_patterns(written: Iterable[str]) -> list[Any]:
    """For every written text with entity tokens: what it looks like filled in."""
    patterns = []
    for text in written:
        if "[[entity:" not in text or len(text) > MAX_CHECKED_TEMPLATE_LENGTH:
            continue
        parts, names, at = [], {}, 0
        for token in _REFERENCE_TOKEN.finditer(text):
            parts.append(re.escape(text[at:token.start()]))
            name = token.group(1)
            if name in names:
                # The same token is the same entity everywhere.
                parts.append(f"(?P={names[name]})")
            else:
                names[name] = f"t{len(names)}"
                parts.append(f"(?P<{names[name]}>{_REFERENCE_FILL})")
            at = token.end()
        if not names or len(names) > 90:
            continue
        parts.append(re.escape(text[at:]))
        patterns.append(re.compile("".join(parts), re.DOTALL))
    return patterns


def filled_from_written(template: str, patterns: Iterable[Any], entities: Iterable[str]) -> bool:
    """Is this template a text the owner wrote with ``[[entity:...]]`` tokens,
    each token filled with a published entity and nothing else changed?"""
    for pattern in patterns:
        found = pattern.fullmatch(template)
        if found and all(entity in entities for entity in found.groupdict().values()):
            return True
    return False


FRIGATE_KINDS = frozenset(
    {
        "thumbnail", "snapshot", "notifications", "clips", "recording", "preview",
        "vod", "jsmpeg", "mse", "webrtc", "go2rtc",
    }
)

ENTITY_PROXIES = {
    "camera_proxy": "camera.",
    "camera_proxy_stream": "camera.",
    "image_proxy": "image.",
    "calendars": "calendar.",
}


def local_path(raw: Any) -> Any:
    """A path to sign, without the address in front of it.

    Some cards ask for a whole address to be signed
    (``https://home.example.com/api/...``). Home Assistant signs the path
    and the query and ignores the rest, so the rest is dropped here, before
    the path is checked: what is checked is then exactly what is signed.
    """
    if not isinstance(raw, str):
        return raw
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https", "ws", "wss") and parts.netloc and not parts.fragment:
        return parts.path + ("?" + parts.query if parts.query else "")
    return raw


class EventMemory:
    """Frigate events (and reviews) already listed to a visitor, with their camera.

    Event ids are not secret enough to be the only protection: a picture is
    served only for an event that came through the camera-filtered list. The
    memory outlives one visitor's connection, so a page reload still shows
    its pictures; it forgets the oldest entries past a fixed size.
    """

    def __init__(self, size: int = MAX_REMEMBERED_EVENTS) -> None:
        self._size = size
        self._events: OrderedDict[str, str] = OrderedDict()

    def note(self, event_id: Any, camera: Any) -> None:
        if not isinstance(event_id, str) or not isinstance(camera, str):
            return
        self._events[event_id] = camera
        self._events.move_to_end(event_id)
        while len(self._events) > self._size:
            self._events.popitem(last=False)

    def camera_of(self, event_id: str) -> str | None:
        return self._events.get(event_id)

    def __len__(self) -> int:
        return len(self._events)


class MediaMemory:
    """Media already listed to a visitor (the pictures of LLM Vision's
    timeline events), so that exactly those can be opened. Bounded like
    EventMemory, and kept across connections for the same reason."""

    def __init__(self, size: int = MAX_REMEMBERED_EVENTS) -> None:
        self._size = size
        self._ids: OrderedDict[str, None] = OrderedDict()

    def note(self, media_content_id: Any) -> None:
        if not isinstance(media_content_id, str) or not media_content_id:
            return
        self._ids[media_content_id] = None
        self._ids.move_to_end(media_content_id)
        while len(self._ids) > self._size:
            self._ids.popitem(last=False)

    def __contains__(self, media_content_id: Any) -> bool:
        return media_content_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


@dataclass
class FrigateAccess:
    """Which Frigate cameras are published."""

    # Every camera each Frigate instance has, by instance id (its MQTT client id).
    instances: dict[str, set[str]] = field(default_factory=dict)
    # Published camera names.
    cameras: set[str] = field(default_factory=set)
    events: EventMemory = field(default_factory=EventMemory)

    @property
    def known(self) -> set[str]:
        return set().union(*self.instances.values()) if self.instances else set()

    def camera_allowed(self, name: Any) -> bool:
        return isinstance(name, str) and name in self.cameras

    def event_allowed(self, event_id: str) -> bool:
        camera = self.events.camera_of(event_id)
        return camera is not None and camera in self.cameras

    def camera_of_file(self, name: str) -> str | None:
        """The camera a Frigate file name belongs to (``Front-1700.1-ab.jpg``,
        ``thumb-Front-1700.1-ab.webp``): the longest camera name it starts
        with, so ``front`` never answers for ``front-door``."""
        if name.startswith("thumb-"):
            name = name[len("thumb-"):]
        best: str | None = None
        for camera in self.known | self.cameras:
            if name.startswith(camera + "-") and (best is None or len(camera) > len(best)):
                best = camera
        return best


@dataclass
class Access:
    """Everything one published dashboard lets a visitor read."""

    entities: set[str] = field(default_factory=set)
    statistics: set[str] = field(default_factory=set)
    # Strings written in the published views, verbatim: the only templates
    # that may be rendered and the only media ids that may be resolved.
    strings: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    camera_streams: bool = True
    frigate: FrigateAccess | None = None
    # UIX foundries (shared card definitions) the published views use; None
    # when UIX is not there to ask, and then nothing is held back.
    foundries: set[str] | None = None
    media: MediaMemory = field(default_factory=MediaMemory)
    hide_location: bool = True
    # Conditions written in the published views (sanitize.collect_conditions).
    conditions: set[str] = field(default_factory=set)
    # Does the link show the energy settings: the Energy dashboard, or an
    # energy card on a published view? If not they are not a visitor's.
    energy: bool = False
    # The written texts with entity tokens, as patterns; made on first use.
    _token_patterns: list[Any] | None = field(default=None, repr=False, compare=False)

    def entity_allowed(self, entity_id: Any) -> bool:
        return isinstance(entity_id, str) and entity_id in self.entities

    # -- templates ------------------------------------------------------------

    def template_verdict(
        self,
        template: Any,
        variables: Any = None,
        read: Callable[[str], tuple[Any, Iterable[str]]] | None = None,
    ) -> tuple[str | None, str]:
        """How a template a visitor's page asks to render is treated, and why
        if it is not rendered.

        ``"written"``: its text is in a published view word for word (or in a
        UIX foundry such a view uses, or it is what UIX composes from such
        text), so the owner wrote it for this page; it is rendered as it is.

        ``"checked"``: some card put it together in the browser. Either it
        is the owner's text with published entities in the place of its
        ``[[entity:...]]`` tokens (``filled_from_written``), or it is read
        first (``assembled_template_problem``) and rendered only if it can
        read nothing but published entities. Rendered, it is watched as well:
        Home Assistant reports which entities a template read, and the result
        is passed on only if every one of them is published
        (``template_reads_allowed``).

        ``None``: not rendered at all.
        """
        if not isinstance(template, str):
            return None, "it is not text"
        if template in self.strings:
            return "written", ""
        if len(template) > MAX_CHECKED_TEMPLATE_LENGTH:
            return None, "it is not written in a published view and is very long"
        if composed_of_written(template, self.strings):
            return "written", ""
        if self._token_patterns is None:
            self._token_patterns = token_patterns(self.strings)
        if filled_from_written(template, self._token_patterns, self.entities):
            # The owner's text with published entities where its tokens
            # were: what it does is the owner's, so it is not read against
            # the list of what an assembled template may use. Which entity
            # stands where is the page's word, so it is watched all the same.
            return "checked", ""
        problem = assembled_template_problem(template, self.entities, variables, read)
        if problem:
            return None, "it is not written in a published view, and " + problem
        return "checked", ""

    def template_trust(self, template: Any, variables: Any = None) -> str | None:
        return self.template_verdict(template, variables)[0]

    def template_reads_allowed(self, listeners: Any) -> bool:
        """Did a watched template read published entities and nothing else?

        ``listeners`` is what Home Assistant sends with every rendered result:
        the entities the template read, whether it went through a whole domain
        or through every state. A walk through a domain or through everything
        reads entities that are not published, so it fails, and so does a
        result that says nothing about what it read."""
        if not isinstance(listeners, dict):
            return False
        if listeners.get("all") or listeners.get("domains"):
            return False
        entities = listeners.get("entities") or ()
        return isinstance(entities, (list, tuple, set, frozenset)) and all(
            self.entity_allowed(e) for e in entities
        )

    # -- conditions ---------------------------------------------------------

    def condition_allowed(self, condition: Any) -> bool:
        """May Home Assistant evaluate this condition for a visitor's page?

        Yes for a state or number comparison of published entities, for
        and/or/not of such, for the frontend's own "has a number" template
        about a published entity, and for any condition the owner wrote in a
        published view exactly as it is asked. A template that is not written
        there is never evaluated: unlike a rendered template, a condition
        does not say what it read."""
        return self._condition_allowed(condition, [MAX_CONDITION_PARTS])

    def _condition_allowed(self, node: Any, left: list[int]) -> bool:
        left[0] -= 1
        if left[0] < 0 or not isinstance(node, dict):
            return False
        kind = node.get("condition")
        if kind in _CONDITION_GROUPS:
            members = node.get("conditions")
            return (
                set(node) <= {"condition", "conditions", "alias", "enabled"}
                and isinstance(node.get("enabled", True), bool)
                and isinstance(members, list)
                and all(self._condition_allowed(member, left) for member in members)
            )
        if json.dumps(node, sort_keys=True, separators=(",", ":"), default=str) in self.conditions:
            return True
        if kind == "template":
            template = node.get("value_template")
            if not set(node) <= {"condition", "value_template", "alias", "enabled"}:
                return False
            if not isinstance(node.get("enabled", True), bool) or not isinstance(template, str):
                return False
            if template in self.strings:
                return True
            made = _IS_NUMBER.match(template)
            if not made:
                return False
            try:
                return self.entity_allowed(json.loads(made.group(1) or made.group(2)))
            except ValueError:
                return False
        keys = _CONDITION_KEYS.get(kind)
        if keys is None or not set(node) <= keys:
            return False
        entity_ids = node.get("entity_id")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not isinstance(entity_ids, list) or not entity_ids:
            return False
        if not all(self.entity_allowed(entity_id) for entity_id in entity_ids):
            return False
        # "for", "enabled" and the like take templates too.
        return not any(_TEMPLATE_MARK.search(text) for text in _strings_of(node))

    # -- media ---------------------------------------------------------------

    def media_allowed(self, media_content_id: Any) -> bool:
        """A media-source id may be resolved if the owner wrote it in a
        published view, or if it is a published camera or image entity."""
        if not isinstance(media_content_id, str):
            return False
        if media_content_id in self.strings or media_content_id in self.media:
            return True
        for prefix in ("media-source://camera/", "media-source://image/"):
            if media_content_id.startswith(prefix):
                return self.entity_allowed(media_content_id[len(prefix):])
        if media_content_id.startswith("media-source://frigate/"):
            return self._frigate_media_allowed(media_content_id[len("media-source://frigate/"):])
        return False

    def _frigate_media_allowed(self, identifier: str) -> bool:
        """A clip, snapshot or recording named the way the Frigate integration
        names them: ``<instance>/event/<clips|snapshots>/<camera>/<event id>``
        or ``<instance>/recordings/<camera>/<day>/<hour>``. Folders (anything
        shorter) are for browsing and are not resolved."""
        frigate = self.frigate
        if frigate is None or not _SAFE_PATH.match("/" + identifier) or ".." in identifier:
            return False
        parts = identifier.split("/")
        if parts and parts[0] in ("event", "recordings"):
            parts = [next(iter(frigate.instances), "")] + parts
        if len(parts) < 2 or parts[0] not in frigate.instances:
            return False
        if parts[1] == "event":
            return (
                len(parts) == 5
                and parts[2] in ("clips", "snapshots")
                and frigate.camera_allowed(parts[3])
                and frigate.events.camera_of(parts[4]) == parts[3]
            )
        if parts[1] == "recordings":
            return len(parts) == 5 and frigate.camera_allowed(parts[2])
        return False

    def path_allowed(self, raw: Any) -> bool:
        """May this path (with its query) be signed or fetched for a visitor?"""
        if not isinstance(raw, str) or not raw.startswith("/api/"):
            return False
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or parts.fragment:
            return False
        path = parts.path
        if "%" in path:
            path = unquote(path)
        if not _SAFE_PATH.match(path) or "//" in path or ".." in path:
            return False
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            # "?src=front&src=bedroom": which one counts depends on who reads it.
            return False
        segments = path.split("/")[2:]  # after "/api/"
        if len(segments) < 2:
            return False
        head, rest = segments[0], segments[1:]
        if head in ENTITY_PROXIES:
            return (
                len(rest) == 1
                and rest[0].startswith(ENTITY_PROXIES[head])
                and self.entity_allowed(rest[0])
            )
        if head == "frigate":
            return self._frigate_path_allowed(rest, query)
        return False

    def _frigate_path_allowed(self, rest: list[str], query: dict[str, str]) -> bool:
        frigate = self.frigate
        if frigate is None or not frigate.cameras or not rest:
            return False
        if rest[0] not in FRIGATE_KINDS:
            # "/api/frigate/<instance>/<kind>/..."
            if rest[0] not in frigate.instances or len(rest) < 2:
                return False
            rest = rest[1:]
        kind, tail = rest[0], [part for part in rest[1:] if part != ""]
        if kind not in FRIGATE_KINDS or not tail:
            return False
        if kind in ("thumbnail", "snapshot"):
            return len(tail) == 1 and frigate.event_allowed(_strip_extension(tail[0]))
        if kind == "notifications":
            return len(tail) >= 2 and frigate.event_allowed(tail[0])
        if kind == "clips":
            cameras = [part for part in tail[:-1] if part in frigate.known]
            if cameras:
                return all(frigate.camera_allowed(camera) for camera in cameras)
            return frigate.camera_allowed(frigate.camera_of_file(tail[-1]))
        if kind in ("recording", "preview", "jsmpeg"):
            return frigate.camera_allowed(tail[0])
        if kind == "vod":
            if tail[0] == "event":
                return len(tail) >= 3 and frigate.event_allowed(tail[1])
            cameras = [part for part in tail[:-1] if part in frigate.known]
            return len(cameras) == 1 and frigate.camera_allowed(cameras[0])
        # A live stream's socket (mse, webrtc, go2rtc/ws) is never signed: a
        # visitor holding a link to it could talk to go2rtc directly, and
        # go2rtc lets a browser send sound to the camera's loudspeaker. It is
        # reached through the public endpoint only (stream_socket_allowed).
        # The rest of go2rtc's own API can describe stream sources, camera
        # logins included: never.
        return False

    def stream_socket_allowed(self, raw: Any) -> bool:
        """Is this the socket of one published live stream
        (``/api/frigate/<instance>/mse/api/ws?src=<stream>``, the same under
        ``webrtc`` or ``go2rtc/ws``)?

        Such a socket is go2rtc's, and go2rtc takes more over it than it
        gives: a browser can offer it a microphone, and it plays that on the
        camera's loudspeaker. So a visitor is never given a link to it. The
        public endpoint opens it for them and passes on only requests to
        watch (``stream_request``)."""
        frigate = self.frigate
        if frigate is None or not frigate.cameras or not isinstance(raw, str):
            return False
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or parts.fragment:
            return False
        path = unquote(parts.path) if "%" in parts.path else parts.path
        if not _SAFE_PATH.match(path) or "//" in path or ".." in path:
            return False
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        # "src" and nothing else: "dst" would make the visitor a source, and
        # a second "src" is read differently by different readers.
        if len(pairs) != 1 or pairs[0][0] != "src":
            return False
        segments = path.split("/")[1:]
        if segments[:2] != ["api", "frigate"]:
            return False
        rest = segments[2:]
        if rest and rest[0] in frigate.instances:
            rest = rest[1:]
        if rest not in (["mse", "api", "ws"], ["webrtc", "api", "ws"], ["go2rtc", "ws", "api", "ws"]):
            return False
        return self._stream_allowed(pairs[0][1])

    # -- LLM Vision -----------------------------------------------------------

    def llmvision_events_allowed(self, raw: Any) -> bool:
        """LLM Vision's timeline (``/api/llmvision/timeline/events``), which
        its cards read over REST: only when such a card is on a published
        view, and then only through the public endpoint, which leaves out
        events of cameras that are not published (``llmvision_events``)."""
        if not isinstance(raw, str):
            return False
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or parts.fragment:
            return False
        if parts.path.rstrip("/") != "/api/llmvision/timeline/events":
            return False
        return any(value.startswith("custom:llmvision-") for value in self.strings)

    def llmvision_events(self, payload: Any) -> dict[str, Any]:
        """The timeline as a visitor gets it: events of published cameras,
        whose pictures are remembered so they can be opened."""
        events = payload.get("events") if isinstance(payload, dict) else None
        kept = []
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict) or not self.entity_allowed(event.get("camera_name")):
                continue
            kept.append(event)
            frame = event.get("key_frame")
            if isinstance(frame, str) and frame.startswith("/media/") and ".." not in frame:
                self.media.note(frame.replace("/media/", "media-source://media_source/local/", 1))
        return {**(payload if isinstance(payload, dict) else {}), "events": kept}

    # -- UIX ------------------------------------------------------------------

    def uix_payload(self, payload: Any) -> Any:
        """What UIX sends its frontend, with the foundries cut down to the
        ones the published views use. Foundries are the owner's shared card
        definitions for every dashboard; the rest are not this page's."""
        if self.foundries is None or not isinstance(payload, dict):
            return payload
        out = dict(payload)
        if isinstance(out.get("foundries"), dict):
            out["foundries"] = {k: v for k, v in out["foundries"].items() if k in self.foundries}
        if isinstance(out.get("result"), dict):
            out["result"] = self.uix_payload(out["result"])
        return out

    def stream_info_allowed(self, raw: Any) -> bool:
        """Is this the description of one published live stream
        (``/api/frigate/<instance>/go2rtc/streams/<stream>``)?

        Camera cards read it to learn whether a stream has sound. It also
        names where the stream comes from — the camera's address on the home
        network — so it is never signed for a visitor to fetch directly: the
        public endpoint fetches it and passes on a cleaned copy
        (``clean_stream_info``)."""
        frigate = self.frigate
        if frigate is None or not isinstance(raw, str) or not self.camera_streams:
            return False
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or parts.fragment or parts.query:
            return False
        path = unquote(parts.path) if "%" in parts.path else parts.path
        if not _SAFE_PATH.match(path) or "//" in path or ".." in path:
            return False
        segments = path.split("/")[1:]
        if segments[:2] != ["api", "frigate"]:
            return False
        rest = segments[2:]
        if rest and rest[0] in frigate.instances:
            rest = rest[1:]
        return len(rest) == 3 and rest[:2] == ["go2rtc", "streams"] and self._stream_allowed(rest[2])

    def _stream_allowed(self, name: str | None) -> bool:
        """A live stream by name. Streams are usually named after their
        camera; another name is accepted only if the owner wrote it in a
        published view and it is not an unpublished camera's."""
        frigate = self.frigate
        if not name or frigate is None or not self.camera_streams:
            return False
        if frigate.camera_allowed(name):
            return True
        return name in self.strings and name not in frigate.known

    # -- Frigate's websocket messages -----------------------------------------

    def frigate_request(self, kind: str, msg: dict[str, Any]) -> dict[str, Any] | None:
        """An allowed Frigate request cut down to the published cameras, or
        None when it must not be forwarded."""
        frigate = self.frigate
        if frigate is None or not frigate.cameras:
            return None
        instance = msg.get("instance_id")
        if instance not in frigate.instances:
            return None
        cameras = frigate.cameras & frigate.instances[instance]
        if not cameras:
            return None
        if kind in ("frigate/events/get", "frigate/reviews/get"):
            wanted = msg.get("cameras")
            if isinstance(wanted, list) and wanted:
                chosen = sorted(c for c in wanted if c in cameras)
                if not chosen:
                    return None
            else:
                chosen = sorted(cameras)
            return {**msg, "cameras": chosen}
        if kind in ("frigate/recordings/summary", "frigate/recordings/get", "frigate/ptz/info"):
            return msg if msg.get("camera") in cameras else None
        return msg

    def frigate_answer(self, kind: str, payload: Any) -> Any:
        """Frigate's answer, checked again on the way out: items of other
        cameras are dropped whatever was asked, and listed events are
        remembered so their pictures can be served. Frigate answers in JSON
        text; the same form is kept."""
        frigate = self.frigate
        if frigate is None:
            return None
        as_text = isinstance(payload, str)
        data = payload
        if as_text:
            try:
                data = json.loads(payload)
            except ValueError:
                return "[]" if kind.endswith("/get") or kind.endswith("/summary") else payload
        if kind in ("frigate/events/get", "frigate/reviews/get", "frigate/events/summary"):
            if not isinstance(data, list):
                return payload
            data = [
                item for item in data
                if isinstance(item, dict) and frigate.camera_allowed(item.get("camera"))
            ]
            if kind != "frigate/events/summary":
                for item in data:
                    frigate.events.note(item.get("id"), item.get("camera"))
                    # A review lists the detections it groups.
                    nested = (item.get("data") or {}) if isinstance(item.get("data"), dict) else {}
                    for detection in nested.get("detections") or []:
                        frigate.events.note(detection, item.get("camera"))
        return json.dumps(data) if as_text else data

    def frigate_event(self, payload: Any) -> Any:
        """A pushed Frigate event or review (``{"before": ..., "after": ...}``):
        passed on only for a published camera, and remembered. Returns None
        to drop it."""
        frigate = self.frigate
        if frigate is None:
            return None
        data = payload
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except ValueError:
                return None
        if not isinstance(data, dict):
            return None
        seen = False
        for key in ("before", "after"):
            item = data.get(key)
            if not isinstance(item, dict):
                continue
            if not frigate.camera_allowed(item.get("camera")):
                return None
            seen = True
            frigate.events.note(item.get("id"), item.get("camera"))
        return payload if seen else None


def _strip_extension(name: str) -> str:
    """``1700000000.123456-abc123.jpg`` -> the event id. The id itself holds
    a dot, so only a known picture or video extension is removed."""
    for extension in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".gif"):
        if name.endswith(extension):
            return name[: -len(extension)]
    return name


def frigate_cameras_in(strings: Iterable[str], known: set[str]) -> set[str]:
    """Frigate camera names written in the published views."""
    return {value for value in strings if value in known}


# What a page may ask of a live stream's socket: to be sent video in one of
# go2rtc's forms, or to set up a WebRTC connection it only receives on.
STREAM_REQUESTS = frozenset({"mse", "mp4", "mjpeg", "hls", "webrtc/offer", "webrtc/candidate"})
MAX_STREAM_REQUEST = 64 * 1024
_SDP_DIRECTIONS = frozenset({"a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"})


def listen_only_sdp(sdp: Any) -> str | None:
    """A WebRTC offer that asks only to receive.

    A browser's offer says, for sound and for picture, whether it wants to
    send as well. One that sends sound is how a visitor would talk through a
    camera's loudspeaker, so every audio and video part is set to receive
    only, whatever it said, and a part that said nothing (which means "both
    ways") is told so too. Parts switched off stay off. None if this is not
    an offer at all."""
    if not isinstance(sdp, str) or "m=" not in sdp or len(sdp) > MAX_STREAM_REQUEST:
        return None
    ending = "\r\n" if "\r\n" in sdp else "\n"
    out: list[str] = []
    media: str | None = None  # kind of the part being read
    stated = False

    def close() -> None:
        if media in ("audio", "video") and not stated:
            out.append("a=recvonly")

    for line in sdp.replace("\r\n", "\n").split("\n"):
        if line.startswith("m="):
            close()
            media, stated = line[2:].split(" ", 1)[0], False
            out.append(line)
        elif line.strip() in _SDP_DIRECTIONS:
            if media is None:
                continue  # for the whole session: each part gets its own
            if media in ("audio", "video"):
                if stated:
                    continue
                stated = True
                out.append("a=inactive" if line.strip() == "a=inactive" else "a=recvonly")
            else:
                out.append(line)
        elif line == "" and media is not None:
            continue  # the blank after the last line; put back below
        else:
            out.append(line)
    close()
    text = ending.join(out)
    return text if text.endswith(ending) else text + ending


def stream_request(text: Any) -> str | None:
    """What a visitor's page sent a live stream's socket, as it may be passed
    on to go2rtc, or None if it may not.

    Passed on: a request for video in one of go2rtc's forms, and the two
    messages that set up WebRTC, the offer made receive-only. Everything
    else go2rtc understands (older message forms among them) is not."""
    if not isinstance(text, str) or len(text) > MAX_STREAM_REQUEST:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    kind = message.get("type") if isinstance(message, dict) else None
    if not isinstance(kind, str) or kind not in STREAM_REQUESTS:
        return None
    value = message.get("value", "")
    if not isinstance(value, str):
        return None
    if message["type"] == "webrtc/offer":
        value = listen_only_sdp(value)
        if value is None:
            return None
    return json.dumps({"type": message["type"], "value": value})


_STREAM_PRIVATE_KEYS = frozenset({"url", "remote_addr", "user_agent", "source", "sdp"})


def clean_stream_info(data: Any) -> Any:
    """go2rtc's description of a stream without where it comes from or who is
    watching: no source addresses (camera logins are already masked by
    Frigate, the camera's address on the home network is not), no viewers'
    addresses. What cards need — which video and audio tracks there are —
    stays."""
    if isinstance(data, dict):
        return {
            key: clean_stream_info(value)
            for key, value in data.items()
            if key not in _STREAM_PRIVATE_KEYS
        }
    if isinstance(data, list):
        return [clean_stream_info(item) for item in data]
    if isinstance(data, str) and "://" in data:
        return ""
    return data


# -- Frigate's own interface -----------------------------------------------------

# What a page of Frigate's interface may send Frigate's socket: requests to be
# told the state of things. Frigate lets a camera's user send "<camera>/ptz"
# as well; a visitor moves no camera.
FRIGATE_SOCKET_TOPICS = frozenset(
    {"onConnect", "modelState", "audioTranscriptionState", "birdseyeLayout",
     "embeddingsReindexProgress", "jobState"}
)

# Addresses of Frigate that a visitor's interface has no business with, on
# top of Frigate's own refusal of them to anyone who is not an administrator:
# signing in and out, users, the configuration as written, logs, exports,
# restarting, probing a camera address, go2rtc's own answer to a WebRTC offer.
_FRIGATE_NEVER = re.compile(
    r"^(?:api/)?(?:login|logout|auth|users?|config/(?:raw|raw_paths|save|set|schema\.json)|logs?|restart"
    r"|exports?|export|debug[\w/]*|metrics|ffprobe|vainfo|nvinfo|chat|go2rtc/webrtc|notifications|reolink)(?:/|$)",
    re.IGNORECASE,
)
_FRIGATE_PATH = re.compile(r"^[A-Za-z0-9_\-./,:@+~ ]*$")
# Pages of Frigate's interface that are not a visitor's: its settings (they
# only change the look in the visitor's own browser, but they are switches,
# and a read-only page has none), and the pages Frigate keeps for
# administrators anyway.
FRIGATE_HIDDEN_PAGES = (
    "settings", "system", "config", "logs", "faces", "classification", "chat", "playground", "replay",
)
_FRIGATE_HIDDEN_PAGE = re.compile(r"^(?:" + "|".join(FRIGATE_HIDDEN_PAGES) + r")(?:/|$)", re.IGNORECASE)
# The gear in Frigate's sidebar and bottom bar, by its drawing.
_FRIGATE_GEAR = 'svg > path[d^="M12.22 2h-.44"]'
_FRIGATE_SECRET_KEYS = frozenset({"password", "api_key", "auth_secret", "secret", "token", "user", "host"})
_FRIGATE_DROPPED = ("environment_vars", "proxy", "tls", "database", "notifications", "telemetry", "logger")


def frigate_ui_path(raw: Any) -> str | None:
    """The address inside Frigate that a request to the public Frigate panel
    is for, or None if it is not passed on."""
    if not isinstance(raw, str):
        return None
    path = unquote(raw) if "%" in raw else raw
    path = path.lstrip("/")
    if not _FRIGATE_PATH.match(path) or ".." in path or "//" in path:
        return None
    if _FRIGATE_NEVER.match(path):
        return None
    return path


def frigate_ui_hidden_page(raw: Any) -> bool:
    """Is this address one of the pages of Frigate's interface a visitor
    does not get (asked for as a page, not as ``api/...``)?"""
    return isinstance(raw, str) and _FRIGATE_HIDDEN_PAGE.match(raw.lstrip("/")) is not None


def frigate_ui_page_like(path: str) -> bool:
    """Would Frigate answer this address with its interface's page (rather
    than data, a picture, a script)?"""
    last = path.rsplit("/", 1)[-1]
    return "." not in last and not path.startswith(("api/", "live/", "vod/", "clips/", "recordings/", "exports/", "ws"))


def frigate_page_guard(base: str) -> str:
    """What is put into Frigate's page before a visitor gets it: its settings
    and account menus and its links to the hidden pages are taken out of
    sight, and a move to one of those pages, however it is made, ends on the
    live view instead. ``base`` is the public address the interface is under
    ("/home_frigate/ui"); only letters, digits, "_" and "/" are in it."""
    if not re.fullmatch(r"/[A-Za-z0-9_/]+", base):
        raise ValueError("unexpected base address")
    hidden = ",".join(f'a[href^="{base}/{page}"]' for page in FRIGATE_HIDDEN_PAGES)
    style = (
        # the settings and account menus at the foot of the sidebar
        "aside>div:last-of-type{display:none!important}"
        # the settings menu wherever else it is put (a phone's bottom bar)
        f"[aria-haspopup]:has({_FRIGATE_GEAR}){{display:none!important}}"
        f"{hidden}{{display:none!important}}"
    )
    pages = "|".join(FRIGATE_HIDDEN_PAGES)
    script = (
        "(function(){"
        f"var base={json.dumps(base)},no=/^\\/(?:{pages})(?:\\/|$)/i;"
        "function bad(u){try{var x=new URL(u,location.href);"
        "return x.origin===location.origin&&x.pathname.indexOf(base+'/')===0&&no.test(x.pathname.slice(base.length));}"
        "catch(e){return false;}}"
        "function home(){location.replace(base+'/');}"
        "['pushState','replaceState'].forEach(function(n){var o=history[n];"
        "history[n]=function(s,t,u){if(u!=null&&bad(u)){home();return;}return o.apply(this,arguments);};});"
        "document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[href]');"
        "if(a&&bad(a.href)){e.preventDefault();e.stopPropagation();}},true);"
        "window.addEventListener('popstate',function(){if(bad(location.href))home();});"
        "if(bad(location.href))home();"
        "})();"
    )
    return f"<style data-public-access>{style}</style><script data-public-access>{script}</script>"


def frigate_socket_message(text: Any) -> str | None:
    """What a visitor's Frigate page sent Frigate's socket, if it may be
    passed on: a request for state, never a command."""
    if not isinstance(text, str) or len(text) > 4096:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    topic = message.get("topic") if isinstance(message, dict) else None
    if not isinstance(topic, str) or topic not in FRIGATE_SOCKET_TOPICS:
        return None
    return json.dumps({"topic": message["topic"], "payload": "", "retain": False})


def _scrub(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: ("" if key in _FRIGATE_SECRET_KEYS and isinstance(value, str) else _scrub(value))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_scrub(item) for item in node]
    if isinstance(node, str) and "://" in node:
        return ""
    return node


def public_frigate_config(config: Any, cameras: Iterable[str]) -> Any:
    """Frigate's configuration as a visitor's interface receives it.

    Frigate already blanks passwords. It still names every camera, and where
    each one is on the home network. Here only the published cameras stay
    (also in groups, streams and the review order), every address is blanked,
    and the parts about the server itself (sign-in, proxy, database, mail,
    environment) are left out."""
    if not isinstance(config, dict):
        return config
    allowed = set(cameras)
    out = {key: value for key, value in config.items() if key not in _FRIGATE_DROPPED}
    if isinstance(out.get("cameras"), dict):
        out["cameras"] = {name: value for name, value in out["cameras"].items() if name in allowed}
    streams = set(allowed)
    for camera in (out.get("cameras") or {}).values():
        live = (camera.get("live") or {}) if isinstance(camera, dict) else {}
        named = live.get("streams")
        streams |= set(named.values()) if isinstance(named, dict) else set()
    go2rtc = out.get("go2rtc")
    if isinstance(go2rtc, dict):
        kept = go2rtc.get("streams") if isinstance(go2rtc.get("streams"), dict) else {}
        out["go2rtc"] = {"streams": {name: [] for name in kept if name in streams}}
    groups = out.get("camera_groups")
    if isinstance(groups, dict):
        cut = {}
        for name, group in groups.items():
            if not isinstance(group, dict):
                continue
            members = [c for c in group.get("cameras") or [] if c in allowed or c == "birdseye"]
            if any(c in allowed for c in members):
                cut[name] = {**group, "cameras": members}
        out["camera_groups"] = cut
    if isinstance(out.get("auth"), dict):
        out["auth"] = {"enabled": bool(out["auth"].get("enabled")), "roles": {"admin": [], "viewer": []}}
    if isinstance(out.get("mqtt"), dict):
        out["mqtt"] = {"enabled": bool(out["mqtt"].get("enabled"))}
    return _scrub(out)


def public_frigate_stats(stats: Any, cameras: Iterable[str]) -> Any:
    """Frigate's statistics with only the published cameras, without the
    commands its processes were started with, and without the graphics and
    neural processors, which are listed by the name of the hardware."""
    if not isinstance(stats, dict):
        return stats
    allowed = set(cameras)
    out = {key: value for key, value in stats.items() if key not in ("gpu_usages", "npu_usages")}
    if isinstance(out.get("cameras"), dict):
        out["cameras"] = {name: value for name, value in out["cameras"].items() if name in allowed}
    for key in ("cpu_usages", "processes"):
        if isinstance(out.get(key), dict):
            out[key] = {
                name: ({k: v for k, v in value.items() if k != "cmdline"} if isinstance(value, dict) else value)
                for name, value in out[key].items()
            }
    return _scrub(out)


def foundries_used(foundries: dict[str, Any], strings: Iterable[str]) -> dict[str, Any]:
    """The UIX foundries the published views use: those named in a view, and
    those named in turn by a foundry already taken."""
    used: dict[str, Any] = {}
    pending = [name for name in set(strings) if name in foundries]
    while pending:
        name = pending.pop()
        if name in used:
            continue
        used[name] = foundries[name]
        pending.extend(n for n in _strings_of(foundries[name]) if n in foundries and n not in used)
    return used


def _strings_of(node: Any) -> set[str]:
    found: set[str] = set()
    stack = [node]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if value:
                found.add(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return found
