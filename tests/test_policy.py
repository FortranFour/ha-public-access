"""What a visitor may fetch besides states: pictures, clips, recordings."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "public_access"))

import policy  # noqa: E402


def access(**changes):
    frigate = policy.FrigateAccess(
        instances={"frigate": {"front", "front-door", "attic", "bedroom"}},
        cameras={"front", "front-door"},
    )
    frigate.events.note("1700000000.1-abc", "front")
    frigate.events.note("1700000000.2-def", "bedroom")
    made = policy.Access(
        entities={"camera.front", "image.front_person", "calendar.bins", "sensor.pv"},
        strings={"front_sub", "media-source://media_source/local/tour.mp4"},
        frigate=frigate,
    )
    for key, value in changes.items():
        setattr(made, key, value)
    return made


ALLOWED = [
    "/api/camera_proxy/camera.front",
    "/api/camera_proxy_stream/camera.front",
    "/api/image_proxy/image.front_person",
    "/api/calendars/calendar.bins?start=2026-11-01T00:00:00Z&end=2026-12-01T00:00:00Z",
    "/api/frigate/frigate/thumbnail/1700000000.1-abc",
    "/api/frigate/frigate/snapshot/1700000000.1-abc.jpg?bbox=1",
    "/api/frigate/frigate/notifications/1700000000.1-abc/snapshot.jpg",
    "/api/frigate/notifications/1700000000.1-abc/clip.mp4",
    "/api/frigate/frigate/vod/event/1700000000.1-abc/index.m3u8",
    "/api/frigate/frigate/vod/front/start/1700000000/end/1700000060/index.m3u8",
    "/api/frigate/frigate/vod/2026-11/15/10/front/America,Los_Angeles/index.m3u8",
    "/api/frigate/frigate/recording/front/start/1700000000/end/1700000060",
    "/api/frigate/frigate/preview/front-door/start/1700000000/end/1700000060",
    "/api/frigate/frigate/clips/review/thumb-front-1700000000.5-rev.webp",
    "/api/frigate/frigate/clips/front-door-1700000000.9-xyz.jpg",
    "/api/frigate/frigate/jsmpeg/front",
]

# A live stream's socket: reached through the public endpoint, never signed.
SOCKETS = [
    "/api/frigate/frigate/mse/api/ws?src=front",
    "/api/frigate/frigate/webrtc/api/ws?src=front-door",
    "/api/frigate/frigate/go2rtc/ws/api/ws?src=front",
    "/api/frigate/mse/api/ws?src=front",
    "/api/frigate/frigate/mse/api/ws?src=front_sub",  # a stream name the owner wrote in a view
]

REFUSED = [
    "/api/states",
    "/api/config",
    "/api/error_log",
    "/api/services/light/turn_on",
    "/api/history/period",  # has its own, filtered, endpoint
    "/api/camera_proxy/camera.garage",
    "/api/camera_proxy/sensor.pv",
    "/api/image_proxy/camera.front",
    "/api/calendars/calendar.private",
    "/api/camera_proxy/camera.front/../../states",
    "/api/camera_proxy/camera.front/%2e%2e/%2e%2e/states",
    "/api/camera_proxy//camera.front",
    "/api/camera_proxy/camera.front#/api/states",
    "https://elsewhere.example/api/camera_proxy/camera.front",
    "//elsewhere.example/api/camera_proxy/camera.front",
    "api/camera_proxy/camera.front",
    "/local/secret.txt",
    "/api/frigate/frigate/thumbnail/1700000000.2-def",  # a bedroom event
    "/api/frigate/frigate/thumbnail/1700000000.3-never-listed",
    "/api/frigate/frigate/notifications/1700000000.2-def/clip.mp4",
    "/api/frigate/frigate/vod/event/1700000000.2-def/index.m3u8",
    "/api/frigate/frigate/vod/bedroom/start/1/end/2/index.m3u8",
    "/api/frigate/frigate/vod/2026-11/15/10/bedroom/utc/index.m3u8",
    "/api/frigate/frigate/vod/2026-11/15/10/index.m3u8",
    "/api/frigate/frigate/recording/bedroom/start/1/end/2",
    "/api/frigate/frigate/clips/review/thumb-bedroom-1700000000.5-rev.webp",
    "/api/frigate/frigate/clips/attic-1700000000.9-xyz.jpg",
    "/api/frigate/frigate/clips/unknown.jpg",
    "/api/frigate/frigate/jsmpeg/bedroom",
    "/api/frigate/frigate/mse/api/ws?src=bedroom",
    "/api/frigate/frigate/mse/api/ws",
    "/api/frigate/frigate/mse/api/ws?src=front&src=bedroom",
    "/api/frigate/frigate/mse/api/ws?src=bedroom&src=front",
    *SOCKETS,  # go2rtc would take a microphone over one
    "/api/frigate/frigate/go2rtc/api/streams",  # can list camera logins
    "/api/frigate/frigate/go2rtc/api/streams?src=front",
    "/api/frigate/other-instance/thumbnail/1700000000.1-abc",
    "/api/frigate/frigate/exports/x.mp4",
    "/api/frigate/frigate",
    None,
    42,
]


@pytest.mark.parametrize("path", ALLOWED)
def test_published_media_may_be_fetched(path):
    assert access().path_allowed(path)


@pytest.mark.parametrize("path", REFUSED)
def test_everything_else_is_refused(path):
    assert not access().path_allowed(path)


def test_a_camera_name_that_starts_another_does_not_answer_for_it():
    """`front` is published, `front-door` is not: its files stay closed."""
    made = access()
    made.frigate.cameras = {"front"}
    assert made.path_allowed("/api/frigate/frigate/clips/front-1700000000.9-xyz.jpg")
    assert not made.path_allowed("/api/frigate/frigate/clips/front-door-1700000000.9-xyz.jpg")
    assert not made.path_allowed("/api/frigate/frigate/clips/review/thumb-front-door-1700000000.9-xyz.webp")


def test_without_frigate_or_without_a_published_camera_nothing_of_frigate_is_served():
    assert not access(frigate=None).path_allowed("/api/frigate/frigate/thumbnail/1700000000.1-abc")
    made = access()
    made.frigate.cameras = set()
    assert not made.path_allowed("/api/frigate/frigate/thumbnail/1700000000.1-abc")
    assert made.frigate_request("frigate/events/get", {"instance_id": "frigate"}) is None


def test_live_streams_can_be_switched_off():
    made = access(camera_streams=False)
    assert not made.stream_socket_allowed("/api/frigate/frigate/mse/api/ws?src=front")
    assert made.path_allowed("/api/frigate/frigate/thumbnail/1700000000.1-abc"), "pictures stay"


def test_media_ids_are_resolved_only_if_written_in_a_view_or_published():
    made = access()
    assert made.media_allowed("media-source://media_source/local/tour.mp4")
    assert made.media_allowed("media-source://camera/camera.front")
    assert made.media_allowed("media-source://frigate/frigate/event/clips/front/1700000000.1-abc")
    assert made.media_allowed("media-source://frigate/frigate/recordings/front/2026-11-15/10")
    for refused in (
        "media-source://media_source/local/private.mp4",
        "media-source://camera/camera.garage",
        "media-source://frigate/frigate/event/clips/bedroom/1700000000.2-def",
        # a listed bedroom event asked for under a published camera's name
        "media-source://frigate/frigate/event/clips/front/1700000000.2-def",
        "media-source://frigate/frigate/event/clips/front/never-listed",
        "media-source://frigate/frigate/recordings/bedroom/2026-11-15/10",
        "media-source://frigate/frigate/recordings/front",  # a folder: browsing
        "media-source://frigate/frigate/event-search/clips",
        "media-source://frigate/elsewhere/recordings/front/2026-11-15/10",
        None,
    ):
        assert not made.media_allowed(refused), refused


def test_frigate_requests_are_cut_down_to_published_cameras():
    made = access()
    ask = lambda kind, **msg: made.frigate_request(kind, {"instance_id": "frigate", **msg})
    assert ask("frigate/events/get")["cameras"] == ["front", "front-door"]
    assert ask("frigate/events/get", cameras=["bedroom", "front"])["cameras"] == ["front"]
    assert ask("frigate/events/get", cameras=["bedroom"]) is None
    assert ask("frigate/reviews/get", cameras=[])["cameras"] == ["front", "front-door"]
    assert ask("frigate/recordings/get", camera="front") is not None
    assert ask("frigate/recordings/get", camera="bedroom") is None
    assert ask("frigate/ptz/info") is None, "no camera named: nothing to ask"
    assert made.frigate_request("frigate/events/get", {"instance_id": "elsewhere"}) is None
    assert made.frigate_request("frigate/events/get", {}) is None


def test_frigate_answers_are_checked_again_and_listed_events_remembered():
    made = access()
    listed = [{"id": "1800.1-new", "camera": "front"}, {"id": "1800.2-new", "camera": "bedroom"}, "junk"]
    out = made.frigate_answer("frigate/events/get", json.dumps(listed))
    assert json.loads(out) == [{"id": "1800.1-new", "camera": "front"}], "same JSON-text form, filtered"
    assert made.frigate.event_allowed("1800.1-new") and not made.frigate.event_allowed("1800.2-new")
    assert made.frigate_answer("frigate/events/get", listed) == [{"id": "1800.1-new", "camera": "front"}]
    review = [{"id": "1900.1-rev", "camera": "front", "data": {"detections": ["1900.5-det"]}}]
    made.frigate_answer("frigate/reviews/get", json.dumps(review))
    assert made.frigate.event_allowed("1900.1-rev") and made.frigate.event_allowed("1900.5-det")
    summary = [{"camera": "front", "count": 2}, {"camera": "bedroom", "count": 9}]
    assert json.loads(made.frigate_answer("frigate/events/summary", json.dumps(summary))) == [{"camera": "front", "count": 2}]
    assert made.frigate_answer("frigate/events/get", "not json") == "[]"


def test_pushed_events_pass_only_for_published_cameras():
    made = access()
    push = lambda camera: json.dumps({"type": "new", "before": {"id": "2000.1-p", "camera": camera},
                                      "after": {"id": "2000.1-p", "camera": camera}})
    assert made.frigate_event(push("bedroom")) is None
    assert not made.frigate.event_allowed("2000.1-p")
    assert made.frigate_event(push("front")) == push("front")
    assert made.frigate.event_allowed("2000.1-p")
    assert made.frigate_event("not json") is None and made.frigate_event(json.dumps({"type": "x"})) is None


def test_the_event_memory_is_bounded():
    memory = policy.EventMemory(size=3)
    for number in range(5):
        memory.note(f"e{number}", "front")
    assert len(memory) == 3 and memory.camera_of("e0") is None and memory.camera_of("e4") == "front"
    memory.note(None, "front"); memory.note("x", None)
    assert len(memory) == 3


def test_camera_names_written_in_a_view_are_found():
    assert policy.frigate_cameras_in({"front", "Front", "light.porch"}, {"front", "attic"}) == {"front"}


# -- templates -------------------------------------------------------------------------


def test_only_a_template_written_in_a_view_is_trusted_as_it_is():
    made = access(strings={"{{ states('lock.safe') }}", "{{ area_entities('kitchen') }}"})
    assert made.template_trust("{{ states('lock.safe') }}") == "written"
    assert made.template_trust("{{ area_entities('kitchen') }}") == "written", "the owner may write anything"
    assert made.template_trust("{{ states('sensor.pv') }}") == "checked"
    assert made.template_trust("{% set a = 1 %}{{ states('lock.safe') }}") == "checked"


@pytest.mark.parametrize("template", [
    "{{ integration_entities('hue') }}",
    "{{ 'hue' | integration_entities }}",
    "{{ area_entities('kitchen') }}",
    "{{ area_devices('kitchen') }}",
    "{{ label_entities('x') }} {{ floor_entities('y') }}",
    "{{ label_devices('x') }}",
    "{{ floor_areas('y') | map('area_entities') | list }}",
    "{{ device_entities('abc') }}",
    "{{ 'abc' | device_entities }}",
    "{{ distance (47.6, -122.3) }}",
    "{{ device_attr('abc', 'name') }} {{ device_id('sensor.pv') }}",
    "{{ config_entry_attr('abc', 'title') }}",
    "{{ issues() }}",
    "{{ distance(47.6, -122.3) }}",
    "{{ closest(states.person) }}",
    "x" * 40000,
    None,
    ["{{ 1 }}"],
])
def test_an_assembled_template_may_not_list_or_locate_what_is_not_published(template):
    assert access().template_trust(template) is None


def test_what_a_watched_template_read_must_be_published():
    made = access()
    read = lambda **kw: {"all": False, "domains": [], "entities": [], "time": False, **kw}
    assert made.template_reads_allowed(read())
    assert made.template_reads_allowed(read(entities=["sensor.pv"], time=True))
    assert made.template_reads_allowed(read(entities={"sensor.pv", "camera.front"})), "Home Assistant hands over sets"
    assert not made.template_reads_allowed(read(entities={"sensor.pv", "lock.safe"}))
    assert not made.template_reads_allowed(read(domains={"light"}))
    assert not made.template_reads_allowed(read(all=True))
    assert not made.template_reads_allowed(None)
    assert not made.template_reads_allowed(read(entities="sensor.pv"))


# -- live streams as camera cards ask for them ------------------------------------------


def test_an_address_is_reduced_to_the_path_that_gets_signed():
    assert policy.local_path("https://home.example.com/api/x?src=a") == "/api/x?src=a"
    assert policy.local_path("wss://home.example.com:8123/api/x") == "/api/x"
    assert policy.local_path("/api/x?src=a") == "/api/x?src=a"
    assert policy.local_path("javascript://home/api/x") == "javascript://home/api/x", "left alone, then refused"
    assert not access().path_allowed(policy.local_path("javascript://home/api/camera_proxy/camera.front"))
    assert access().path_allowed(policy.local_path("http://192.168.1.2:8123/api/camera_proxy/camera.front"))
    assert not access().path_allowed(policy.local_path("http://192.168.1.2:8123/api/states"))
    assert policy.local_path(None) is None


def test_a_streams_description_is_only_for_published_streams_and_never_signed():
    made = access()
    for path in ("/api/frigate/frigate/go2rtc/streams/front", "/api/frigate/go2rtc/streams/front-door",
                 "/api/frigate/frigate/go2rtc/streams/front_sub"):
        assert made.stream_info_allowed(path), path
        assert not made.path_allowed(path), "served cleaned, never signed"
    for path in ("/api/frigate/frigate/go2rtc/streams/bedroom", "/api/frigate/frigate/go2rtc/streams",
                 "/api/frigate/frigate/go2rtc/streams/front/extra", "/api/frigate/frigate/go2rtc/streams/front?x=1",
                 "/api/frigate/frigate/go2rtc/api/streams", "/api/frigate/elsewhere/go2rtc/streams/front",
                 "/api/frigate/frigate/go2rtc/streams/../streams/front", None):
        assert not made.stream_info_allowed(path), path
    assert not access(camera_streams=False).stream_info_allowed("/api/frigate/frigate/go2rtc/streams/front")


def test_a_streams_description_loses_where_it_comes_from_and_who_watches():
    info = {
        "producers": [{"url": "rtsp://*:*@10.0.4.109:554/Streaming/Channels/101", "remote_addr": "10.0.4.109:554",
                       "medias": ["video, recvonly, H264", "audio, recvonly, PCMA/8000"],
                       "receivers": [{"codec": {"codec_name": "h264"}, "bytes": 5}]}],
        "consumers": [{"remote_addr": "192.168.0.50:51234", "user_agent": "Mozilla", "medias": ["video, sendonly, H264"]}],
        "note": "from rtsp://10.0.4.109/x",
    }
    assert policy.clean_stream_info(info) == {
        "producers": [{"medias": ["video, recvonly, H264", "audio, recvonly, PCMA/8000"],
                       "receivers": [{"codec": {"codec_name": "h264"}, "bytes": 5}]}],
        "consumers": [{"medias": ["video, sendonly, H264"]}],
        "note": "",
    }
    assert "10.0.4.109" not in str(policy.clean_stream_info(info))


def test_an_entity_or_a_word_named_like_such_a_function_is_no_concern():
    """`sensor.unavailable_entities` is an entity, not the listing of entities."""
    made = access()
    for template in (
        "{{ states('sensor.unavailable_entities') }}",
        "{{ states.sensor.device_id.state }} {{ state_attr('sensor.pv', 'device_id') }}",
        "{% set distance = 3 %}{{ distance }} {{ 'closest' }} {{ x.issues }}",
        "N/A - {% set e = states('sensor.unavailable_entities') | int(0) %}{{ e }} {{ 'Entity' if e == 1 else 'Entities' }}",
    ):
        assert made.template_trust(template) == "checked", template


# -- what UIX composes from the owner's text -------------------------------------------

BODY = "{{ 'lit' if is_state(e, 'on') else 'dark' }}"
TAIL = "{{ label }} {{ onoff('light.porch') }} {{ states.lock | list }}"
WRITTEN = {BODY, TAIL, "{% do returns(1) %}", "tools.jinja", "{{ pre }} badge"}
MACRO = "{% macro onoff(e) %}" + BODY + "{% endmacro %}\n"
BILLET = '{%- set label = "Kitchen is" -%}\n'


def test_macros_and_values_in_front_of_the_owners_template_are_the_owners():
    for template in (
        MACRO + BILLET + TAIL,
        BILLET + TAIL,
        MACRO + TAIL,
        "{% macro macro_c(a = 1, b = \"x\", returns) %}{% do returns(1) %}{% endmacro %}\n{% set c = macro_c | as_function %}\n" + TAIL,
        "{% from 'tools.jinja' import helper %}\n" + TAIL,
        '{%- set d = {"a": [1, 2.5, true, none, "x -%} y"]} -%}\n{%- set e = "it\\"s" -%}\n' + TAIL,
    ):
        assert policy.composed_of_written(template, WRITTEN), template
        assert access(strings=WRITTEN).template_trust(template) == "written"


@pytest.mark.parametrize("template", [
    TAIL,                                                                 # nothing composed: plain lookup decides
    MACRO + BILLET + "{{ states('lock.safe') }}",                         # an ending the owner did not write
    MACRO + BILLET + TAIL + "{{ states('lock.safe') }}",
    "{% macro onoff(e) %}{{ states('lock.safe') }}{% endmacro %}\n" + TAIL,  # a body the owner did not write
    "{% macro onoff(e = states('lock.safe')) %}" + BODY + "{% endmacro %}\n" + TAIL,
    '{%- set label = states("lock.safe") -%}\n' + TAIL,                   # a value that is not a plain value
    '{%- set label = "x" ~ states("lock.safe") -%}\n' + TAIL,
    '{%- set label = "{{ 1 }}" -%}\n' + TAIL,
    "{{ states('lock.safe') }}" + BILLET + TAIL,                          # something in front
    BILLET + "{{ states('lock.safe') }}\n" + TAIL,                        # something in between
    BILLET.rstrip("\n") + TAIL,                                           # not how UIX joins them
    "{% from 'secrets.jinja' import helper %}\n" + TAIL,                  # a file the owner did not name
    "{% set label = 'x' %}\n" + TAIL,                                     # not UIX's wording
])
def test_anything_else_around_it_is_not(template):
    assert not policy.composed_of_written(template, WRITTEN)


def test_foundries_are_taken_only_when_a_view_names_them():
    foundries = {
        "badge": {"forge": {"mold": "badge"}, "element": {"name": "{{ a }}"}, "foundry": "base"},
        "base": {"forge": {"macros": {}}, "element": {"entity": "sensor.pv"}},
        "unused": {"element": {"content": "the alarm code"}},
    }
    used = policy.foundries_used(foundries, {"badge", "custom:uix-forge", "something"})
    assert set(used) == {"badge", "base"}, "named in a view, and named by one already taken"
    assert policy.foundries_used(foundries, {"nothing"}) == {}


def test_uix_sends_a_visitor_only_the_foundries_in_use():
    made = access(foundries={"badge"})
    sent = made.uix_payload({"result": {"foundries": {"badge": {"a": 1}, "unused": {"b": 2}}, "version": "9"}})
    assert sent == {"result": {"foundries": {"badge": {"a": 1}}, "version": "9"}}
    assert made.uix_payload({"foundries": {"badge": 1, "unused": 2}}) == {"foundries": {"badge": 1}}
    everything = {"result": {"foundries": {"x": 1}}}
    assert access(foundries=None).uix_payload(everything) == everything, "UIX could not be asked: nothing held back"


# -- LLM Vision ------------------------------------------------------------------------


def test_the_timeline_is_read_only_where_its_card_is_published():
    path = "/api/llmvision/timeline/events?limit=5"
    assert not access().llmvision_events_allowed(path)
    made = access(strings={"custom:llmvision-card"})
    assert made.llmvision_events_allowed(path) and made.llmvision_events_allowed("/api/llmvision/timeline/events")
    assert not made.path_allowed(path), "served filtered, never signed"
    for other in ("/api/llmvision/timeline/event/1", "/api/llmvision/settings", "https://x/api/llmvision/timeline/events", None):
        assert not made.llmvision_events_allowed(other), other


def test_the_timeline_holds_published_cameras_and_their_pictures_only():
    made = access()
    timeline = {"events": [
        {"uid": "1", "camera_name": "camera.front", "key_frame": "/media/llmvision/snapshots/a.jpg"},
        {"uid": "2", "camera_name": "camera.bedroom", "key_frame": "/media/llmvision/snapshots/b.jpg"},
        {"uid": "3", "camera_name": "", "key_frame": "/media/llmvision/snapshots/c.jpg"},
        {"uid": "4", "camera_name": "camera.front", "key_frame": "/media/../secrets.yaml"},
        "junk",
    ]}
    assert [e["uid"] for e in made.llmvision_events(timeline)["events"]] == ["1", "4"]
    assert made.media_allowed("media-source://media_source/local/llmvision/snapshots/a.jpg")
    assert not made.media_allowed("media-source://media_source/local/llmvision/snapshots/b.jpg")
    assert not made.media_allowed("media-source://media_source/local/llmvision/snapshots/c.jpg")
    assert not made.media_allowed("media-source://media_source/local/../secrets.yaml")
    assert made.llmvision_events(None) == {"events": []}


def test_the_media_memory_is_bounded():
    memory = policy.MediaMemory(size=2)
    for name in ("a", "b", "c", None, ""):
        memory.note(name)
    assert len(memory) == 2 and "a" not in memory and "c" in memory


# -- conditions (conditional cards, "visibility") ------------------------------

PLAIN_CONDITIONS = [
    {"condition": "state", "entity_id": "sensor.pv", "state": "on"},
    {"condition": "state", "entity_id": ["sensor.pv", "calendar.bins"], "state": ["on", "off"], "attribute": "mode"},
    {"condition": "state", "entity_id": "sensor.pv", "state": "on", "for": {"minutes": 5}},
    {"condition": "numeric_state", "entity_id": "sensor.pv", "above": 3, "below": 9.5},
    {"condition": "not", "conditions": [{"condition": "state", "entity_id": "sensor.pv", "state": "unavailable"}]},
    {"condition": "or", "conditions": [
        {"condition": "numeric_state", "entity_id": "sensor.pv", "above": 3},
        {"condition": "and", "conditions": []},
    ]},
    {"condition": "template", "value_template": '{{ is_number(states("sensor.pv")) }}'},
    {"condition": "template", "value_template": '{{ is_number(state_attr("sensor.pv", "level")) }}'},
]


@pytest.mark.parametrize("condition", PLAIN_CONDITIONS)
def test_a_plain_condition_about_published_entities_is_evaluated(condition):
    assert access().condition_allowed(condition)


@pytest.mark.parametrize("condition", [
    {"condition": "state", "entity_id": "lock.safe", "state": "locked"},
    {"condition": "state", "entity_id": ["sensor.pv", "lock.safe"], "state": "on"},
    {"condition": "state", "entity_id": [], "state": "on"},
    {"condition": "state", "state": "on"},
    {"condition": "numeric_state", "entity_id": "sensor.salary", "above": 50000},
    {"condition": "numeric_state", "entity_id": "sensor.pv", "above": 3, "value_template": "{{ states('sensor.salary') }}"},
    {"condition": "state", "entity_id": "sensor.pv", "state": "on", "for": "{{ states('sensor.salary') }}"},
    {"condition": "state", "entity_id": "sensor.pv", "state": "on", "enabled": "{{ is_state('lock.safe', 'locked') }}"},
    {"condition": "not", "conditions": [{"condition": "state", "entity_id": "lock.safe", "state": "locked"}]},
    {"condition": "or", "conditions": "{{ is_state('lock.safe', 'locked') }}"},
    {"condition": "or", "conditions": ["{{ is_state('lock.safe', 'locked') }}"]},
    {"condition": "and", "conditions": [], "enabled": "{{ is_state('lock.safe', 'locked') }}"},
    {"condition": "template", "value_template": "{{ is_state('lock.safe', 'locked') }}"},
    {"condition": "template", "value_template": '{{ is_number(states("sensor.salary")) }}'},
    {"condition": "template", "value_template": '{{ is_number(states("sensor.pv")) }}{{ is_state("lock.safe", "locked") }}'},
    {"condition": "zone", "entity_id": "person.owner", "zone": "zone.home"},
    {"condition": "device", "device_id": "abc", "domain": "lock", "type": "is_locked"},
    {"condition": "lock.is_locked", "target": {"area_id": "hall"}},
    "{{ is_state('lock.safe', 'locked') }}",
    [{"condition": "state", "entity_id": "sensor.pv", "state": "on"}],
    None,
])
def test_any_other_condition_is_not(condition):
    assert not access().condition_allowed(condition)


def test_a_condition_the_owner_wrote_is_evaluated_as_it_is():
    written = {"condition": "template", "value_template": "{{ is_state('sensor.pv', 'on') and now().hour > 6 }}"}
    sun = {"condition": "sun", "after": "sunset"}
    made = access(
        strings={written["value_template"]},
        conditions={json.dumps(sun, sort_keys=True, separators=(",", ":"))},
    )
    assert made.condition_allowed(written)
    assert made.condition_allowed({"after": "sunset", "condition": "sun"})
    assert made.condition_allowed({"condition": "and", "conditions": [sun, written]})
    assert not made.condition_allowed({"condition": "sun", "after": "sunrise"})
    assert not made.condition_allowed({**written, "value_template": written["value_template"] + " "})


def test_a_condition_cannot_be_made_endlessly_large():
    one = {"condition": "state", "entity_id": "sensor.pv", "state": "on"}
    assert access().condition_allowed({"condition": "and", "conditions": [one] * 150})
    assert not access().condition_allowed({"condition": "and", "conditions": [one] * 250})


# -- live streams: to watch and to listen to, never to talk through ------------

@pytest.mark.parametrize("path", SOCKETS)
def test_a_published_streams_socket_is_served_by_the_public_endpoint(path):
    assert access().stream_socket_allowed(path)
    assert not access().path_allowed(path), "and never signed"


@pytest.mark.parametrize("path", [
    "/api/frigate/frigate/mse/api/ws?src=bedroom",
    "/api/frigate/frigate/mse/api/ws",
    "/api/frigate/frigate/mse/api/ws?dst=front",                 # would make the visitor a source
    "/api/frigate/frigate/mse/api/ws?src=front&dst=front",
    "/api/frigate/frigate/mse/api/ws?src=front&src=bedroom",
    "/api/frigate/frigate/mse/api/ws?src=front&video=copy",
    "/api/frigate/frigate/mse/api/ws?src=rtsp://10.0.0.9/stream",  # go2rtc would open it
    "/api/frigate/frigate/mse/api/streams?src=front",
    "/api/frigate/frigate/mse/api/webrtc?src=front",
    "/api/frigate/frigate/go2rtc/api/ws?src=front",
    "/api/frigate/frigate/go2rtc/ws/api/streams?src=front",
    "/api/frigate/frigate/mse/api/../api/ws?src=front",
    "/api/frigate/elsewhere/mse/api/ws?src=front",
    "https://example.com/api/frigate/frigate/mse/api/ws?src=front",
    "/api/frigate/frigate/jsmpeg/front",
    None,
])
def test_nothing_else_is_a_stream_socket(path):
    assert not access().stream_socket_allowed(path)


OFFER = "\r\n".join([
    "v=0", "o=- 1 2 IN IP4 127.0.0.1", "s=-", "t=0 0", "a=sendrecv", "a=group:BUNDLE 0 1 2 3",
    "m=video 9 UDP/TLS/RTP/SAVPF 96", "c=IN IP4 0.0.0.0", "a=mid:0", "a=recvonly", "a=rtpmap:96 H264/90000",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111", "a=mid:1", "a=sendrecv", "a=rtpmap:111 opus/48000/2", "a=sendonly",
    "m=audio 9 UDP/TLS/RTP/SAVPF 0", "a=mid:2", "a=rtpmap:0 PCMU/8000",
    "m=audio 0 UDP/TLS/RTP/SAVPF 8", "a=mid:4", "a=inactive",
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel", "a=mid:3", "",
])


def _directions(sdp):
    parts, kind = [], None
    for line in sdp.split("\r\n"):
        if line.startswith("m="):
            kind = line[2:].split(" ")[0]
            parts.append([kind])
        elif line in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
            (parts[-1] if parts else parts).append(line[2:])
    return parts


def test_a_webrtc_offer_is_made_receive_only():
    made = policy.listen_only_sdp(OFFER)
    assert _directions(made) == [
        ["video", "recvonly"],
        ["audio", "recvonly"],     # said "both ways", and then "send"
        ["audio", "recvonly"],     # said nothing, which means both ways
        ["audio", "inactive"],     # off stays off
        ["application"],
    ]
    assert "send" not in made and made.endswith("\r\n")
    assert made.count("\r\n") == OFFER.count("\r\n") - 2 + 1, "one line less for the session, one less doubled, one added"
    assert "a=rtpmap:111 opus/48000/2" in made and "a=group:BUNDLE 0 1 2 3" in made
    assert policy.listen_only_sdp(made) == made
    bare = policy.listen_only_sdp(OFFER.replace("\r\n", "\n"))
    assert "\r" not in bare and bare.replace("\n", "\r\n") == made, "line endings are kept as they came"


@pytest.mark.parametrize("sdp", [None, 5, "", "hello", {"sdp": OFFER}, "m=audio 9 x 0\r\n" * 20000])
def test_what_is_not_an_offer_is_not_passed_on(sdp):
    assert policy.listen_only_sdp(sdp) is None


def test_a_visitor_may_ask_a_stream_only_to_watch():
    assert json.loads(policy.stream_request('{"type":"mse","value":"avc1.640029,mp4a.40.2"}')) == {
        "type": "mse", "value": "avc1.640029,mp4a.40.2"}
    assert json.loads(policy.stream_request('{"type":"mse"}')) == {"type": "mse", "value": ""}
    candidate = '{"type":"webrtc/candidate","value":"candidate:1 1 udp 2 192.0.2.1 5000 typ host"}'
    assert json.loads(policy.stream_request(candidate))["value"].startswith("candidate:1")
    offered = json.loads(policy.stream_request(json.dumps({"type": "webrtc/offer", "value": OFFER, "extra": 1})))
    assert set(offered) == {"type", "value"} and "send" not in offered["value"]


@pytest.mark.parametrize("text", [
    '{"type":"webrtc","value":{"type":"offer","sdp":"m=audio"}}',   # go2rtc's older form
    '{"type":"webrtc/offer","value":"not an offer"}',
    '{"type":"webrtc/offer","value":{"sdp":"m=audio 9 x 0"}}',
    '{"type":"webrtc/answer","value":"m=audio 9 x 0"}',
    '{"type":"mse","value":["avc1"]}',
    '{"type":"ptz","value":"left"}',
    '{"type":["mse"],"value":"x"}', '{"type":{"a":1}}',
    '{"type":"mse"} {"type":"webrtc"}',
    '["mse"]', "mse", "", None, b"\x00\x01",
    '{"type":"mse","value":"' + "a" * 70000 + '"}',
])
def test_anything_else_sent_to_a_stream_is_dropped(text):
    assert policy.stream_request(text) is None


# -- Frigate's own interface, read-only ----------------------------------------

@pytest.mark.parametrize("path", [
    "", "review", "assets/index-abc.js", "locales/en/common.json", "api/config", "api/profile", "api/stats",
    "api/events/1700000000.1-ab/thumbnail.webp", "api/front/latest.webp", "api/review", "api/front/recordings",
    "vod/front/start/1/end/2/master.m3u8", "clips/front-1.jpg", "live/jsmpeg/front", "live/mse/api/ws", "ws",
])
def test_reading_frigates_interface_is_passed_on(path):
    assert policy.frigate_ui_path(path) == path
    assert policy.frigate_ui_path("/" + path) == path


@pytest.mark.parametrize("path", [
    "api/login", "login", "api/logout", "logout", "api/auth", "api/auth/first_time_login", "api/users", "api/users/visitor/password",
    "api/config/raw", "api/config/raw_paths", "api/config/save", "api/config/set", "api/config/schema.json",
    "api/logs/frigate", "logs", "api/restart", "api/export/front/start/1/end/2", "export", "exports/a.mp4",
    "api/go2rtc/webrtc", "api/ffprobe", "api/vainfo", "api/metrics", "api/chat/completion", "api/debug_replay/start",
    "API/Login", "api/../api/login", "api//config", "api/config/../login", "api/front/latest.jpg?x=1", "api/a%2e%2e/b", "api/<script>", None, 5,
])
def test_the_rest_of_frigate_is_not(path):
    assert policy.frigate_ui_path(path) is None


def test_frigates_socket_is_asked_for_state_never_commanded():
    passed = policy.frigate_socket_message('{"topic":"onConnect","payload":"x","retain":true,"extra":1}')
    assert json.loads(passed) == {"topic": "onConnect", "payload": "", "retain": False}
    for topic in ("front/ptz", "front/detect/set", "front/recordings/set", "restart", "front/motion_threshold/set", "notifications/set", ""):
        assert policy.frigate_socket_message(json.dumps({"topic": topic, "payload": "ON"})) is None, topic
    for text in ("onConnect", "[]", '{"topic":["onConnect"]}', None, b"x", '{"topic":"onConnect","payload":"' + "a" * 5000 + '"}'):
        assert policy.frigate_socket_message(text) is None


FRIGATE_CONFIG = {
    "cameras": {name: {"name": name, "ffmpeg": {"inputs": [{"path": f"rtsp://*:*@10.0.4.{n}:554/ch1"}]},
                       "live": {"streams": {"Main": name, "Sub": name + "_sub"}}, "onvif": {"host": f"10.0.4.{n}", "user": "admin", "password": "*"}}
                for n, name in enumerate(["front", "bedroom", "garage"])},
    "go2rtc": {"streams": {"front": ["rtsp://*:*@10.0.4.0/x"], "front_sub": ["rtsp://x"], "bedroom": ["rtsp://y"], "spare": ["rtsp://z"]}, "api": {"listen": ":1984"}},
    "camera_groups": {"out": {"cameras": ["front", "garage", "bedroom", "birdseye"], "order": 0}, "in": {"cameras": ["bedroom"], "order": 1}},
    "mqtt": {"enabled": True, "host": "core-mosquitto", "user": "frigate", "password": "*"},
    "auth": {"enabled": True, "roles": {"admin": [], "viewer": [], "family": ["bedroom"]}, "cookie_name": "frigate_token"},
    "proxy": {"auth_secret": "*"}, "environment_vars": {"K": "v"}, "tls": {"enabled": True}, "database": {"path": "/config/frigate.db"},
    "genai": {"provider": "openai", "base_url": "https://api.example.com/v1", "api_key": "*"},
    "ui": {"timezone": "Europe/Rome"}, "version": "0.18.2",
}


def test_frigates_configuration_is_cut_down_to_the_published_cameras():
    out = policy.public_frigate_config(FRIGATE_CONFIG, {"front", "garage", "not_in_frigate"})
    text = json.dumps(out)
    assert sorted(out["cameras"]) == ["front", "garage"]
    assert out["go2rtc"] == {"streams": {"front": [], "front_sub": []}}
    assert out["camera_groups"] == {"out": {"cameras": ["front", "garage", "birdseye"], "order": 0}}
    assert out["mqtt"] == {"enabled": True} and out["auth"] == {"enabled": True, "roles": {"admin": [], "viewer": []}}
    assert out["ui"] == {"timezone": "Europe/Rome"} and out["version"] == "0.18.2"
    for gone in ("bedroom", "10.0.4", "rtsp", "core-mosquitto", "environment_vars", "auth_secret", "frigate.db", "api.example.com", "family", "spare", '"tls"'):
        assert gone not in text, gone
    assert out["cameras"]["front"]["onvif"] == {"host": "", "user": "", "password": ""}
    assert FRIGATE_CONFIG["cameras"]["bedroom"] and FRIGATE_CONFIG["mqtt"]["host"] == "core-mosquitto", "the original is untouched"
    assert policy.public_frigate_config(["not", "a", "config"], {"front"}) == ["not", "a", "config"]


def test_frigates_statistics_lose_other_cameras_and_command_lines():
    stats = {"cameras": {"front": {"camera_fps": 5}, "bedroom": {"camera_fps": 5}},
             "cpu_usages": {"12": {"cpu": "1", "cmdline": "ffmpeg -i rtsp://admin:pw@10.0.4.9/x"}, "frigate.full_system": {"cpu": "2"}},
             "service": {"version": "0.18.2", "storage": {"/media/frigate/recordings": {"total": 1}}}}
    out = policy.public_frigate_stats(stats, {"front"})
    assert list(out["cameras"]) == ["front"] and "cmdline" not in json.dumps(out) and "pw@" not in json.dumps(out)
    assert out["cpu_usages"]["12"] == {"cpu": "1"} and out["service"]["version"] == "0.18.2"
