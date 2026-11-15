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
    "/api/frigate/frigate/mse/api/ws?src=front",
    "/api/frigate/frigate/webrtc/api/ws?src=front-door",
    "/api/frigate/frigate/go2rtc/ws/api/ws?src=front",
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
    assert not made.path_allowed("/api/frigate/frigate/mse/api/ws?src=front")
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
    "{{ device_entities('abc') }}",
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
