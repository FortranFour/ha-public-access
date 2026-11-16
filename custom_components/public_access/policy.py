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
from typing import Any, Iterable
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
# ... and may not use what reads the house without reading an entity's state,
# which is the one thing the watch below cannot see: lists of entities and
# devices by area, floor, label, device or integration, device and config
# entry details, repair issues, and distances (measured from the home, they
# would locate it).
_UNWATCHABLE_NAMES = (
    r"(?:integration|device|area|floor|label)_entities|(?:area|label)_devices"
    r"|(?:floor|label)_areas|device_attr|is_device_attr|device_id|device_name"
    r"|config_entry_id|config_entry_attr|issues?|is_hidden_entity|distance|closest"
)
# The name as a function call, or as a filter after a pipe. Not as part of an
# entity id ("sensor.unavailable_entities") or as a plain word in a string.
_UNWATCHABLE = re.compile(
    rf"(?<![.\w])(?:{_UNWATCHABLE_NAMES})\s*\(|\|\s*(?:{_UNWATCHABLE_NAMES})\b"
)

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

    def entity_allowed(self, entity_id: Any) -> bool:
        return isinstance(entity_id, str) and entity_id in self.entities

    # -- templates ------------------------------------------------------------

    def template_trust(self, template: Any) -> str | None:
        """How a template a visitor's page asks to render is treated.

        ``"written"``: its text is in a published view word for word (or in a
        UIX foundry such a view uses, or it is what UIX composes from such
        text), so the owner wrote it for this page; it is rendered as it is.

        ``"checked"``: some card put it together in the browser. It may be
        rendered, but only under watch: Home Assistant reports which entities
        a template read, and the result is passed on only if every one of them
        is published (``template_reads_allowed``).

        ``None``: not rendered at all.
        """
        if not isinstance(template, str):
            return None
        if template in self.strings:
            return "written"
        if len(template) > MAX_CHECKED_TEMPLATE_LENGTH:
            return None
        if composed_of_written(template, self.strings):
            return "written"
        if _UNWATCHABLE.search(template):
            return None
        return "checked"

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
        if kind in ("mse", "webrtc") or (kind == "go2rtc" and tail[0] == "ws"):
            return self._stream_allowed(query.get("src"))
        # The rest of go2rtc's own API can describe stream sources, camera
        # logins included: never.
        return False

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
