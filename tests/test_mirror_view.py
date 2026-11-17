"""The mirror publishes exactly the chosen views, and refuses what a viewer may not ask."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("homeassistant")

from custom_components.public_access import mirror, mirror_core  # noqa: E402
from custom_components.public_access.coordinator import PublicDashboardCoordinator  # noqa: E402
from custom_components.public_access.mirror import MirrorSession  # noqa: E402
from custom_components.public_access.policy import Access, FrigateAccess  # noqa: E402


class _Entry:
    def __init__(self, **options):
        self.data, self.options = {}, options


def _coordinator(**options):
    coordinator = PublicDashboardCoordinator.__new__(PublicDashboardCoordinator)
    coordinator.entry = _Entry(dashboard="energy-public", public_path="solar", **options)
    return coordinator


CONFIG = {"views": [
    {"path": "overview", "cards": []},
    {"path": 123456, "cards": []},
    {"path": "costs", "cards": []},
]}


def _paths(config):
    return [(v["path"], v.get("visible", True)) for v in config["views"]]


def test_only_the_chosen_view_is_sent():
    assert _paths(_coordinator(view_path="123456").publish(CONFIG)) == [(123456, True)]


def test_several_chosen_views_are_sent_in_dashboard_order():
    published = _coordinator(views=["costs", "overview"]).publish(CONFIG)
    assert _paths(published) == [("overview", True), ("costs", True)]


def test_a_supporting_view_is_sent_hidden():
    published = _coordinator(views=["costs"], support_views=["overview"]).publish(CONFIG)
    assert _paths(published) == [("overview", False), ("costs", True)]


def test_a_missing_view_publishes_nothing_not_the_first_one():
    """Never fall back to another view: it was not chosen and may be private."""
    assert _coordinator(view_path="gone").publish(CONFIG)["views"] == []
    assert _coordinator(views=["gone"]).publish(CONFIG)["views"] == []


def test_supporting_views_alone_publish_nothing():
    """A supporting view only helps a published one; without one, nothing is sent."""
    assert _coordinator(views=["gone"], support_views=["overview"]).publish(CONFIG)["views"] == []


def test_a_published_view_the_owner_hides_is_still_published():
    config = {"views": [{"path": "kiosk", "visible": False, "cards": []}]}
    assert _paths(_coordinator(views=["kiosk"]).publish(config)) == [("kiosk", False)]


def test_the_tab_bar_shows_only_with_several_views():
    assert not _coordinator(view_path="costs").show_header
    assert _coordinator(views=["costs", "overview"]).show_header
    assert not _coordinator(views=["costs", "overview"], show_tabs=False).show_header


def test_the_frontend_glue_ships_with_the_integration():
    """Since 0.5 nothing is downloaded: mirror_core is part of the package."""
    from custom_components.public_access import assets

    core = assets.mirror_core()
    assert core.API_VERSION == 1
    assert callable(core.assemble_page)
    for name in ("inbound", "outbound", "local_answer", "silent_subscription", "dashboards"):
        assert hasattr(core.Session, name), name


# -- what a visitor's messages do ---------------------------------------------------


class _Wire:
    """Stands in for the visitor's socket and for Home Assistant: records what
    is answered here and what is forwarded."""

    def __init__(self):
        self.answers, self.forwarded = [], []
        self.subscriptions, self.ended = {}, []

    def send_str(self, text):
        self.answers.append(json.loads(text))

    def create_task(self, _coro):
        return None

    def async_handle(self, msg):
        self.forwarded.append(msg)


def _session(access):
    wire = _Wire()
    session = MirrorSession.__new__(MirrorSession)
    session._ws = session._loop = session._connection = wire
    session._hass = type("Hass", (), {"states": type("States", (), {"get": staticmethod(
        lambda entity_id: object() if entity_id in EXISTING else None)})})()
    session._public_path, session._dashboard, session._title = "solar", "energy-public", "Solar"
    session._access_now = lambda: access
    session._last_id, session._awaiting, session._read_template, session._subjects = 0, set(), None, {}
    session._publish = _coordinator(views=["overview"]).publish
    session._types, session._watched, session.notes = {}, {}, []
    session._conditions = set()
    session._asked = {}
    session._note = session.notes.append
    session.refused, session.page_errors = [], []
    session._notes = type("Notes", (), {
        "note_refusal": session.notes.append,
        "note_refused_type": session.refused.append,
        "note_page_error": session.page_errors.append,
    })()
    session._core = mirror_core.Session(public_path="solar", dashboard="energy-public")
    session._error = lambda msg_id, code, message: {
        "id": msg_id, "type": "result", "success": False, "error": {"code": code, "message": message}}
    return session, wire


# Entities Home Assistant has; only some are published.
EXISTING = {"sensor.pv", "camera.front", "light.porch", "lock.safe", "sensor.secret"}


def _access():
    frigate = FrigateAccess(instances={"frigate": {"Front", "Bedroom"}}, cameras={"Front"})
    frigate.events.note("1700.1-ab", "Front")
    frigate.events.note("1700.2-cd", "Bedroom")
    return Access(
        entities={"sensor.pv", "camera.front", "light.porch"},
        statistics={"sensor.pv"},
        strings={"{{ states('sensor.pv') }}"},
        devices={"dev1"},
        frigate=frigate,
    )


def _ask(access=None, **msg):
    session, wire = _session(access or _access())
    session.handle({"id": 7, **msg})
    return session, wire


def _refused(wire):
    return (not wire.forwarded and len(wire.answers) == 1
            and wire.answers[0]["success"] is False
            and wire.answers[0]["error"]["code"] == "unauthorized")


@pytest.mark.parametrize("msg", [
    {"type": "call_service", "domain": "light", "service": "turn_on"},
    {"type": "lovelace/config/save", "config": {}},
    {"type": "frigate/event/retain", "instance_id": "frigate", "event_id": "1700.1-ab", "retain": True},
    {"type": "frigate/reviews/viewed", "instance_id": "frigate", "ids": ["x"]},
    {"type": "config/entity_registry/update", "entity_id": "light.porch"},
    {"type": "media_source/browse_media"},
    {"type": "render_template", "template": "{{ integration_entities('hue') }}"},
    {"type": "render_template", "template": "{{ 'kitchen' | area_entities }}"},
    {"type": "render_template", "template": "{{ distance(47.6, -122.3) }}"},
    {"type": "render_template", "template": "{{ device_attr('abc', 'name') }}"},
    {"type": "render_template", "template": {"not": "text"}},
    {"type": "uix/set_foundry", "name": "x", "config": {}},
    {"type": "uix/delete_foundry", "name": "x"},
    {"type": "auth/sign_path", "path": "/api/states"},
    {"type": "auth/sign_path", "path": "https://home.example.com/api/states"},
    {"type": "auth/sign_path", "path": "https://home.example.com/api/frigate/frigate/mse/api/ws?src=Bedroom"},
    {"type": "auth/sign_path", "path": "/api/frigate/frigate/go2rtc/api/streams"},
    {"type": "auth/sign_path", "path": "/api/camera_proxy/camera.garage"},
    {"type": "auth/sign_path", "path": "/api/frigate/frigate/thumbnail/1700.2-cd"},
    {"type": "auth/long_lived_access_token", "client_name": "x", "lifespan": 365},
    {"type": "camera/stream", "entity_id": "camera.garage"},
    {"type": "camera/stream", "entity_id": "light.porch"},
    {"type": "weather/subscribe_forecast", "entity_id": "weather.home", "forecast_type": "daily"},
    {"type": "config/entity_registry/get", "entity_id": "sensor.secret"},
    {"type": "recorder/statistic_during_period", "statistic_id": "sensor.secret"},
    {"type": "frigate/events/get", "instance_id": "frigate", "cameras": ["Bedroom"]},
    {"type": "frigate/recordings/summary", "instance_id": "frigate", "camera": "Bedroom"},
    {"type": "frigate/events/get", "instance_id": "elsewhere"},
    {"type": "media_source/resolve_media", "media_content_id": "media-source://media_source/local/private.mp4"},
    {"type": "media_source/resolve_media", "media_content_id": "media-source://frigate/frigate/event/clips/Bedroom/1700.2-cd"},
    {"type": "subscribe_condition", "condition": {"condition": "state", "entity_id": "lock.safe", "state": "locked"}},
    {"type": "subscribe_condition", "condition": {"condition": "numeric_state", "entity_id": "sensor.secret", "above": 50}},
    {"type": "subscribe_condition", "condition": {"condition": "template", "value_template": "{{ is_state('lock.safe', 'locked') }}"}},
    {"type": "subscribe_condition", "condition": {"condition": "state", "entity_id": "sensor.pv", "state": "sensor.secret"}},
    {"type": "subscribe_condition", "condition": {"condition": "numeric_state", "entity_id": "sensor.pv", "above": "sensor.secret"}},
    {"type": "subscribe_condition", "condition": "{{ is_state('lock.safe', 'locked') }}"},
    {"type": "subscribe_condition"},
    {"type": "test_condition", "condition": {"condition": "state", "entity_id": "sensor.pv", "state": "on"}},
])
def test_refused_before_it_reaches_home_assistant(msg):
    _, wire = _ask(**msg)
    assert _refused(wire), (wire.answers, wire.forwarded)


@pytest.mark.parametrize("msg", [
    {"type": "render_template", "template": "{{ states('sensor.pv') }}"},
    {"type": "auth/sign_path", "path": "/api/camera_proxy/camera.front"},
    {"type": "auth/sign_path", "path": "/api/frigate/frigate/thumbnail/1700.1-ab"},
    {"type": "camera/stream", "entity_id": "camera.front"},
    {"type": "config/entity_registry/get", "entity_id": "camera.front"},
    {"type": "frigate/recordings/summary", "instance_id": "frigate", "camera": "Front"},
    {"type": "media_source/resolve_media", "media_content_id": "media-source://frigate/frigate/event/clips/Front/1700.1-ab"},
    {"type": "map_tiles/access_token"},
    {"type": "entity/source"},
    {"type": "uix/connect", "browserID": "x"},
    {"type": "uix/get_foundries"},
    {"type": "subscribe_condition", "condition": {"condition": "state", "entity_id": "light.porch", "state": "on"}},
    {"type": "subscribe_condition", "condition": {"condition": "not", "conditions": [
        {"condition": "numeric_state", "entity_id": "sensor.pv", "above": 3}]}},
])
def test_forwarded_when_it_is_about_something_published(msg):
    _, wire = _ask(**msg)
    assert not wire.answers and len(wire.forwarded) == 1


def test_frigate_events_are_asked_for_published_cameras_only():
    _, wire = _ask(type="frigate/events/get", instance_id="frigate")
    assert wire.forwarded[0]["cameras"] == ["Front"]
    _, wire = _ask(type="frigate/events/get", instance_id="frigate", cameras=["Bedroom", "Front"])
    assert wire.forwarded[0]["cameras"] == ["Front"]


def test_a_signed_link_never_outlives_an_hour():
    _, wire = _ask(type="auth/sign_path", path="/api/camera_proxy/camera.front", expires=10**9)
    assert wire.forwarded[0]["expires"] == 3600


def test_with_live_streams_off_a_camera_shows_stills():
    access = _access()
    access.camera_streams = False
    _, wire = _ask(access, type="camera/stream", entity_id="camera.front")
    assert _refused(wire)
    _, wire = _ask(access, type="camera/capabilities", entity_id="camera.front")
    assert wire.answers[0]["result"] == {"frontend_stream_types": []} and not wire.forwarded


def test_the_logbook_is_read_by_published_entity_only():
    _, wire = _ask(type="logbook/get_events", start_time="2026-01-01T00:00:00Z")
    assert wire.answers[0]["result"] == [] and not wire.forwarded
    _, wire = _ask(type="logbook/get_events", start_time="x", entity_ids=["light.porch", "lock.safe"], device_ids=["d"])
    assert wire.forwarded[0]["entity_ids"] == ["light.porch"] and "device_ids" not in wire.forwarded[0]


def test_other_dashboards_do_not_exist():
    _, wire = _ask(type="lovelace/dashboards/list")
    assert [d["url_path"] for d in wire.answers[0]["result"]] == ["solar"] and not wire.forwarded
    _, wire = _ask(type="lovelace/config", url_path="dashboard-private")
    assert wire.forwarded[0]["url_path"] == "energy-public"


def test_cancelling_a_quiet_subscription_is_answered_here():
    """persistent_notification/subscribe is answered without Home Assistant, so
    its cancellation must be too: forwarded, it came back as an error."""
    session, wire = _ask(type="persistent_notification/subscribe")
    session.handle({"id": 8, "type": "unsubscribe_events", "subscription": 7})
    assert [a["success"] for a in wire.answers] == [True, True] and not wire.forwarded


# -- what Home Assistant's answers look like to a visitor -----------------------------


def _answer(kind, result=None, event=None, access=None):
    session, _ = _session(access or _access())
    session._types[7] = kind
    if event is not None:
        return session._filter_outbound({"id": 7, "type": "event", "event": event})
    return session._filter_outbound({"id": 7, "type": "result", "success": True, "result": result})


def test_the_home_is_not_located():
    out = _answer("get_config", {"latitude": 47.6, "longitude": -122.3, "location_name": "Elm Street",
                                 "config_dir": "/config", "time_zone": "America/Los_Angeles",
                                 "whitelist_external_dirs": ["/config/www"], "external_url": "https://x.example"})
    assert out["result"] == {"latitude": 0, "longitude": 0, "location_name": "Solar", "config_dir": "",
                             "time_zone": "America/Los_Angeles", "whitelist_external_dirs": [], "external_url": None}


def test_devices_are_cut_down_and_lose_their_hardware_addresses():
    out = _answer("config/device_registry/list", [
        {"id": "dev1", "name": "Inverter", "connections": [["mac", "aa:bb"]], "identifiers": [["x", "serial"]],
         "serial_number": "S123", "configuration_url": "http://192.168.1.5", "via_device_id": "dev9"},
        {"id": "dev2", "name": "Marshall's iPhone"},
    ])
    assert [d["id"] for d in out["result"]] == ["dev1"]
    device = out["result"][0]
    assert device["name"] == "Inverter" and device["connections"] == [] and device["identifiers"] == []
    assert device["serial_number"] is None and device["configuration_url"] is None and device["via_device_id"] is None


def test_frigate_answers_are_filtered_and_remembered():
    access = _access()
    listed = json.dumps([{"id": "1800.1-new", "camera": "Front"}, {"id": "1800.2-new", "camera": "Bedroom"}])
    out = _answer("frigate/events/get", listed, access=access)
    assert json.loads(out["result"]) == [{"id": "1800.1-new", "camera": "Front"}]
    assert access.path_allowed("/api/frigate/frigate/snapshot/1800.1-new")
    assert not access.path_allowed("/api/frigate/frigate/snapshot/1800.2-new")


def test_pushed_frigate_events_of_other_cameras_are_dropped():
    push = lambda camera: json.dumps({"type": "new", "before": {"id": "1", "camera": camera}, "after": {"id": "1", "camera": camera}})
    assert _answer("frigate/events/subscribe", event=push("Bedroom")) is None
    assert _answer("frigate/events/subscribe", event=push("Front"))["event"] == push("Front")


def test_edits_to_other_dashboards_are_not_announced():
    event = lambda url_path: {"event_type": "lovelace_updated", "data": {"url_path": url_path}}
    assert _answer("subscribe_events", event=event("dashboard-private")) is None
    assert _answer("subscribe_events", event=event("energy-public"))["event"]["data"] == {"url_path": "solar"}


def test_the_logbook_does_not_say_who_did_it():
    out = _answer("logbook/get_events", [{"entity_id": "light.porch", "state": "on", "context_user_id": "abc", "context_name": "Marshall"}])
    assert out["result"] == [{"entity_id": "light.porch", "state": "on"}]


# -- templates a card put together in the browser ----------------------------------------

COMPOSED = "{% set on = is_state('light.porch', 'on') %}{{ 'lit' if on else 'dark' }}"


def _watched(template=COMPOSED, **extra):
    session, wire = _ask(type="render_template", template=template, **extra)
    wire.subscriptions[7] = lambda: wire.ended.append(7)
    return session, wire


def _result(session, entities=(), domains=(), everything=False):
    return session._filter_outbound({"id": 7, "type": "event", "event": {
        "result": "x", "listeners": {"all": everything, "entities": set(entities), "domains": set(domains), "time": False}}})


def test_a_template_written_in_a_view_is_rendered_as_it_is():
    session, wire = _ask(type="render_template", template="{{ states('sensor.pv') }}")
    assert wire.forwarded == [{"id": 7, "type": "render_template", "template": "{{ states('sensor.pv') }}"}]
    assert not session._watched


def test_an_assembled_template_is_rendered_under_watch_and_against_the_clock():
    session, wire = _watched()
    assert wire.forwarded[0]["timeout"] == 5 and 7 in session._watched
    _, wire = _watched(timeout=60)
    assert wire.forwarded[0]["timeout"] == 5
    _, wire = _watched(timeout=2)
    assert wire.forwarded[0]["timeout"] == 2


def test_its_result_is_passed_on_when_it_read_only_published_entities():
    session, wire = _watched()
    out = _result(session, entities={"light.porch", "sensor.pv"})
    assert out["event"]["result"] == "x"
    assert out["event"]["listeners"]["entities"] == ["light.porch", "sensor.pv"], "lists, as Home Assistant sends them"
    assert not wire.ended and not session.notes


@pytest.mark.parametrize("read", [
    {"entities": {"light.porch", "lock.safe"}},
    {"domains": {"light"}},
    {"everything": True},
])
def test_its_result_is_withheld_when_it_read_anything_else(read):
    session, wire = _watched()
    out = _result(session, **read)
    assert "result" not in out["event"] and out["event"]["level"] == "ERROR"
    assert wire.ended == [7], "and the subscription is ended"
    assert 7 not in session._watched and len(session.notes) == 1
    assert "lock.safe" in session.notes[0] or "every entity" in session.notes[0]


def test_a_result_that_does_not_say_what_it_read_is_withheld():
    session, wire = _watched()
    out = session._filter_outbound({"id": 7, "type": "event", "event": {"result": "x"}})
    assert "result" not in out["event"] and wire.ended == [7]


def test_an_assembled_template_is_rendered_without_home_assistants_error_texts():
    """An error can quote what the template read ("float got invalid input
    'armed_away'"), so none is asked for and none is passed on."""
    session, wire = _watched(report_errors=True, strict=True)
    assert wire.forwarded[0]["report_errors"] is False and wire.forwarded[0]["strict"] is False
    out = session._filter_outbound({"id": 7, "type": "event", "event": {
        "error": "ValueError: float got invalid input 'armed_away'", "level": "ERROR"}})
    assert out["event"] == {"error": mirror.TEMPLATE_NOT_RENDERED, "level": "ERROR"}
    failed = session._filter_outbound({"id": 7, "type": "result", "success": False,
                                       "error": {"code": "template_error", "message": "got 'armed_away'"}})
    assert failed["error"]["message"] == mirror.TEMPLATE_NOT_RENDERED and 7 not in session._watched


def test_withheld_and_failed_look_the_same_to_the_visitor():
    session, _ = _watched()
    withheld = _result(session, entities={"lock.safe"})
    session, _ = _watched()
    failed = session._filter_outbound({"id": 7, "type": "event", "event": {"error": "x", "level": "ERROR"}})
    assert withheld == failed


@pytest.mark.parametrize("template", [
    "{{ states('lock.safe') }}",
    "{{ states('lock.' ~ 'safe') }}",
    "{% set f = integration_entities %}{{ f('hue') }}",
    "{{ entity_name('lock.safe') }}",
    "{{ areas() }}",
])
def test_an_assembled_template_that_could_read_more_is_not_run_at_all(template):
    session, wire = _ask(type="render_template", template=template)
    assert _refused(wire) and not session._watched
    assert "Template not rendered for a visitor: it is not written in a published view" in session.notes[0]


def test_a_written_templates_error_is_passed_on_as_home_assistant_words_it():
    session, _ = _ask(type="render_template", template="{{ states('sensor.pv') }}")
    error = {"id": 7, "type": "event", "event": {"error": "UndefinedError: x", "level": "ERROR"}}
    assert session._filter_outbound(dict(error)) == error


def test_cancelling_a_watched_template_before_it_started_does_not_lift_the_watch():
    """Home Assistant starts the subscription only after a first trial
    render. Cancelled in between, it answers "not found" and starts it
    afterwards all the same; its results must still be checked then."""
    session, wire = _session(_access())
    session.handle({"id": 1, "type": "render_template", "template": COMPOSED})
    session.handle({"id": 2, "type": "unsubscribe_events", "subscription": 1})
    assert 1 in session._watched, "no answer yet: the subscription may still start"
    wire.subscriptions[1] = lambda: wire.ended.append(1)
    session._filter_outbound({"id": 1, "type": "result", "success": True, "result": None})
    out = session._filter_outbound({"id": 1, "type": "event", "event": {
        "result": "secret", "listeners": {"all": False, "entities": {"lock.safe"}, "domains": set(), "time": False}}})
    assert "result" not in out["event"] and wire.ended == [1]


def test_a_watch_is_let_go_once_the_subscription_is_really_gone():
    session, wire = _session(_access())
    session.handle({"id": 1, "type": "render_template", "template": COMPOSED})
    wire.subscriptions[1] = lambda: None
    session._filter_outbound({"id": 1, "type": "result", "success": True, "result": None})
    del wire.subscriptions[1]  # Home Assistant ends it on the visitor's word
    session.handle({"id": 2, "type": "unsubscribe_events", "subscription": 1})
    assert not session._watched


# -- every answer goes through the filter of its own request ---------------------------


def test_an_id_already_used_is_turned_down_and_changes_nothing():
    """Sent again under the id of a running subscription, a harmless request
    would otherwise put its own kind in place of the subscription's, and the
    subscription's events would then go out unfiltered."""
    session, wire = _session(_access())
    session.handle({"id": 5, "type": "subscribe_events", "event_type": "state_changed"})
    session.handle({"id": 5, "type": "ping"})
    session.handle({"id": 4, "type": "get_states"})
    session.handle({"id": 5, "type": "persistent_notification/subscribe"})
    assert [m["type"] for m in wire.forwarded] == ["subscribe_events"]
    assert [a["error"]["code"] for a in wire.answers] == ["id_reuse"] * 3
    assert session._types == {5: "subscribe_events"}
    secret = {"id": 5, "type": "event", "event": {"event_type": "state_changed", "data": {
        "entity_id": "lock.safe", "new_state": {"state": "unlocked"}}}}
    assert session._filter_outbound(secret) is None
    session.handle({"id": 6, "type": "ping"})
    assert wire.forwarded[-1] == {"id": 6, "type": "ping"}


@pytest.mark.parametrize("bad", [0, -3, True, "8", 2.5, None, [8]])
def test_an_id_that_is_no_positive_number_gets_nowhere(bad):
    session, wire = _session(_access())
    session.handle({"id": bad, "type": "get_states"})
    assert not wire.forwarded and not session._types and wire.answers[0]["success"] is False


def test_an_answer_to_nothing_asked_from_here_is_not_passed_on():
    session, wire = _session(_access())
    states = [{"entity_id": "lock.safe", "state": "unlocked"}]
    assert session._filter_outbound({"id": 9, "type": "result", "success": True, "result": states}) is None
    assert session._filter_outbound({"type": "event", "event": {"data": states}}) is None
    assert session._filter_outbound({"id": "9", "type": "result", "success": False, "error": {}}) is None
    session._send("not json at all")
    session._send(json.dumps([{"id": 9, "type": "result", "success": True, "result": states}]))
    assert not wire.answers


def test_an_event_is_passed_on_only_for_a_request_that_has_events():
    session, _ = _session(_access())
    session._types.update({1: "get_states", 2: "ping", 3: "subscribe_entities", 4: "lovelace/config"})
    leak = {"type": "event", "event": {"a": {"lock.safe": {"s": "unlocked"}}}}
    assert session._filter_outbound({"id": 1, **leak}) is None
    assert session._filter_outbound({"id": 2, **leak}) is None
    assert session._filter_outbound({"id": 4, **leak}) is None
    assert session._filter_outbound({"id": 3, **leak})["event"] == {"a": {}}
    assert session._filter_outbound({"id": 2, "type": "pong"}) == {"id": 2, "type": "pong"}
    assert session._filter_outbound({"id": 1, "type": "pong"}) is None


# -- what is published now, not what was when the page connected --------------------------


def test_an_open_page_is_held_to_what_is_published_now():
    now = {"access": _access()}
    session, wire = _session(now["access"])
    session._access_now = lambda: now["access"]
    session.handle({"id": 1, "type": "subscribe_entities"})
    assert wire.forwarded[0]["entity_ids"] == ["camera.front", "light.porch", "sensor.pv"]
    # The owner takes the porch light off the dashboard.
    now["access"] = Access(entities={"sensor.pv"}, statistics={"sensor.pv"})
    out = session._filter_outbound({"id": 1, "type": "event", "event": {
        "c": {"light.porch": {"+": {"s": "on"}}, "sensor.pv": {"+": {"s": "4"}}}}})
    assert out["event"]["c"] == {"sensor.pv": {"+": {"s": "4"}}}
    session.handle({"id": 2, "type": "history/history_during_period", "entity_ids": ["light.porch"]})
    session.handle({"id": 3, "type": "render_template", "template": "{{ states('light.porch') }}"})
    assert [m["id"] for m in wire.forwarded] == [1], "no longer published"


# -- frontend settings and the energy settings ---------------------------------------------


def test_system_data_is_the_same_few_keys_read_or_subscribed_to():
    for kind in ("frontend/get_system_data", "frontend/subscribe_system_data"):
        session, wire = _ask(type=kind, key="core")
        assert wire.forwarded and not wire.answers, kind
    session, wire = _ask(type="frontend/get_system_data", key="home")
    assert _refused(wire)
    session, wire = _ask(type="frontend/subscribe_system_data", key="home")
    assert not wire.forwarded
    assert wire.answers == [{"id": 7, "type": "event", "event": {"value": None}},
                            {"id": 7, "type": "result", "success": True, "result": None}]
    session.handle({"id": 8, "type": "unsubscribe_events", "subscription": 7})
    assert not wire.forwarded and wire.answers[-1]["success"] is True
    session, wire = _ask(type="frontend/subscribe_system_data")
    assert not wire.forwarded, "no key: not everything"


def _energy(shown):
    access = _access()
    access.energy = shown
    access.statistics |= {"sensor.grid", "sensor.co2"}
    return access


@pytest.mark.parametrize("kind", ["energy/get_prefs", "energy/info", "energy/validate", "energy/solar_forecast"])
def test_the_energy_settings_are_a_visitors_only_if_the_link_shows_energy(kind):
    session, wire = _ask(_energy(False), type=kind)
    assert not wire.forwarded and wire.answers[0]["success"] is False
    session, wire = _ask(_energy(True), type=kind)
    assert wire.forwarded == [{"id": 7, "type": kind}]


def test_without_energy_the_page_is_told_there_are_no_settings():
    """Home Assistant's own answer where none are set; the frontend expects it."""
    _, wire = _ask(_energy(False), type="energy/get_prefs")
    assert wire.answers[0]["error"] == {"code": "not_found", "message": "No prefs"}
    for kind in ("frontend/get_system_data", "frontend/subscribe_system_data"):
        _, wire = _ask(_energy(False), type=kind, key="energy")
        assert not wire.forwarded
        _, wire = _ask(_energy(True), type=kind, key="energy")
        assert wire.forwarded


def test_fossil_consumption_is_worked_out_from_published_statistics_only():
    ask = dict(type="energy/fossil_energy_consumption", start_time="2026-01-01T00:00:00Z",
               end_time="2026-01-02T00:00:00Z", period="day")
    _, wire = _ask(_energy(True), **ask, energy_statistic_ids=["sensor.grid"], co2_statistic_id="sensor.co2")
    assert wire.forwarded
    _, wire = _ask(_energy(True), **ask, energy_statistic_ids=["sensor.grid", "sensor.secret"], co2_statistic_id="sensor.co2")
    assert _refused(wire)
    _, wire = _ask(_energy(True), **ask, energy_statistic_ids=["sensor.grid"], co2_statistic_id="sensor.secret")
    assert _refused(wire)
    _, wire = _ask(_energy(True), **ask, energy_statistic_ids=["sensor.grid"])
    assert _refused(wire)


# -- signing for cards that ask in their own ways ------------------------------------------


def test_a_whole_address_is_signed_as_its_path():
    _, wire = _ask(type="auth/sign_path", path="https://home.example.com/api/frigate/frigate/jsmpeg/Front")
    assert wire.forwarded[0]["path"] == "/api/frigate/frigate/jsmpeg/Front"


def test_a_live_streams_socket_is_never_signed():
    """go2rtc would take a microphone over it. The page is sent to the public
    endpoint instead, which passes on only requests to watch."""
    for asked in ("/api/frigate/frigate/mse/api/ws?src=Front",
                  "https://home.example.com/api/frigate/frigate/webrtc/api/ws?src=Front"):
        _, wire = _ask(type="auth/sign_path", path=asked)
        assert not wire.forwarded
        assert wire.answers[0]["result"] == {"path": "/solar" + asked[asked.index("/api/"):]}
    _, wire = _ask(type="auth/sign_path", path="/api/frigate/frigate/mse/api/ws?dst=Front")
    assert not wire.forwarded and _refused(wire)


def test_home_assistants_own_webrtc_is_made_receive_only():
    offer = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=sendrecv\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    _, wire = _ask(type="camera/webrtc/offer", entity_id="camera.front", offer=offer)
    sent = wire.forwarded[0]["offer"]
    assert "send" not in sent and sent.count("a=recvonly") == 2
    _, wire = _ask(type="camera/webrtc/offer", entity_id="camera.front", offer="nonsense")
    assert not wire.forwarded and _refused(wire)


def test_a_streams_description_is_served_cleaned_never_signed():
    _, wire = _ask(type="auth/sign_path", path="/api/frigate/frigate/go2rtc/streams/Front")
    assert not wire.forwarded
    assert wire.answers[0]["result"] == {"path": "/solar/api/frigate/frigate/go2rtc/streams/Front"}
    _, wire = _ask(type="auth/sign_path", path="/api/frigate/frigate/go2rtc/streams/Bedroom")
    assert _refused(wire)


def test_a_cards_note_for_the_owners_log_is_dropped():
    _, wire = _ask(type="uix/log", message="x")
    assert wire.answers[0]["success"] is True and not wire.forwarded


def test_a_written_template_cannot_be_pointed_at_another_entity():
    """ "{{ states(entity) }}" is the owner's, for the entity on the card. A
    visitor who sends it with another entity in its variables is refused."""
    access = _access()
    access.strings.add("{{ states(entity) }} {{ states(config.entity) }}")
    template = "{{ states(entity) }} {{ states(config.entity) }}"
    _, wire = _ask(access, type="render_template", template=template,
                   variables={"entity": "light.porch", "config": {"entity": "sensor.pv", "icon": "mdi:home", "file": "plan.png"}})
    assert len(wire.forwarded) == 1
    for variables in ({"entity": "lock.safe"}, {"config": {"entity": "sensor.pv", "more": ["x", {"y": "sensor.secret"}]}}):
        session, wire = _ask(access, type="render_template", template=template, variables=variables)
        assert _refused(wire) and session.notes, variables


def test_uix_is_sent_only_the_foundries_in_use():
    access = _access()
    access.foundries = {"badge"}
    out = _answer("uix/connect", event={"result": {"foundries": {"badge": 1, "unused": 2}}}, access=access)
    assert out["event"] == {"result": {"foundries": {"badge": 1}}}
    out = _answer("uix/get_foundries", {"foundries": {"badge": 1, "unused": 2}}, access=access)
    assert out["result"] == {"foundries": {"badge": 1}}


def test_the_coordinates_stay_when_the_owner_says_so():
    access = _access()
    access.hide_location = False
    out = _answer("get_config", {"latitude": 47.6, "longitude": -122.3, "location_name": "Elm Street", "config_dir": "/config"}, access=access)
    assert out["result"] == {"latitude": 47.6, "longitude": -122.3, "location_name": "Solar", "config_dir": ""}


# -- what the owner is told ------------------------------------------------------------


def test_every_refused_request_is_counted_for_the_owner():
    session, _ = _ask(type="config/zone/list")
    assert session.refused == ["config/zone/list"]
    session, _ = _ask(type="camera/stream", entity_id="camera.garage")
    assert session.refused == ["camera/stream (camera not on a published view)"]


def test_the_page_reports_its_own_errors_and_nothing_is_forwarded():
    session, wire = _ask(type="public_access/page_error", kind="rejection", view="/solar/controls",
                         text="TypeError: x is undefined | at card.js:1" + "y" * 2000)
    assert not wire.forwarded and wire.answers[0]["success"] is True
    assert len(session.page_errors) == 1
    assert session.page_errors[0].startswith("/solar/controls [rejection] TypeError: x is undefined")
    assert len(session.page_errors[0]) < 800, "a visitor cannot fill the owner's diagnostics"
    session, wire = _ask(type="public_access/page_error", text={"not": "text"})
    assert not session.page_errors and not wire.forwarded


def test_which_integration_an_entity_is_from_is_told_for_published_entities_only():
    _, wire = _ask(type="entity/source")
    assert wire.forwarded[0]["entity_ids"] == ["camera.front", "light.porch", "sensor.pv"]
    _, wire = _ask(type="entity/source", entity_ids=["sensor.pv", "lock.safe"])
    assert wire.forwarded[0]["entity_ids"] == ["sensor.pv"]
    out = _answer("entity/source", {"sensor.pv": {"domain": "solar"}, "lock.safe": {"domain": "zwave"}})
    assert out["result"] == {"sensor.pv": {"domain": "solar"}}


def test_a_refused_condition_is_noted_for_the_owner():
    session, wire = _ask(type="subscribe_condition",
                         condition={"condition": "state", "entity_id": "lock.safe", "state": "locked"})
    assert _refused(wire)
    assert "lock.safe" in session.notes[0] and session.refused == ["subscribe_condition (not from a published view)"]


def test_a_page_cannot_have_conditions_watched_without_end(monkeypatch):
    from custom_components.public_access import mirror
    monkeypatch.setattr(mirror, "MAX_CONDITIONS", 2)
    session, wire = _session(_access())
    condition = {"condition": "state", "entity_id": "light.porch", "state": "on"}
    for msg_id in (1, 2, 3):
        session.handle({"id": msg_id, "type": "subscribe_condition", "condition": condition})
        session._filter_outbound({"id": msg_id, "type": "result", "success": True, "result": None})
    assert [m["id"] for m in wire.forwarded] == [1, 2] and wire.answers[0]["success"] is False
    session.handle({"id": 4, "type": "unsubscribe_events", "subscription": 1})
    session.handle({"id": 5, "type": "subscribe_condition", "condition": condition})
    assert [m["id"] for m in wire.forwarded] == [1, 2, 4, 5]
    # One that Home Assistant turned down does not count either.
    session._filter_outbound({"id": 5, "type": "result", "success": False, "error": {"code": "invalid_format"}})
    session.handle({"id": 6, "type": "subscribe_condition", "condition": condition})
    assert wire.forwarded[-1]["id"] == 6


# -- a link with several panels ------------------------------------------------

def _site_session():
    from sanitize import Panel
    session, wire = _session(_access())
    session._site = [
        Panel(path="solar", dashboard="energy-public", title="Solar", views=("overview",)),
        Panel(path="solar_guests", dashboard="dashboard-guest", title="Guests", views=("__all__",)),
        Panel(path="solar_energy", kind="energy", dashboard="energy"),
        Panel(path="solar_frigate", kind="frigate", title="Frigate"),
    ]
    return session, wire


def test_every_panel_of_the_link_is_in_the_sidebar_and_nothing_else():
    session, _ = _site_session()
    owners = {
        "energy-public": {"component_name": "lovelace", "icon": "mdi:solar-power", "title": "Real name", "url_path": "energy-public"},
        "dashboard-guest": {"component_name": "lovelace", "icon": "mdi:account", "title": "Guest", "url_path": "dashboard-guest"},
        "dashboard-private": {"component_name": "lovelace", "title": "Private", "url_path": "dashboard-private"},
        "energy": {"component_name": "energy", "icon": "mdi:lightning-bolt", "title": "energy", "url_path": "energy", "require_admin": False},
        "config": {"component_name": "config", "title": "config", "url_path": "config", "require_admin": True},
    }
    panels = session._panels(owners)
    assert list(panels) == ["solar", "solar_guests", "solar_energy", "solar_frigate"]
    assert panels["solar"]["title"] == "Solar" and panels["solar"]["url_path"] == "solar"
    assert panels["solar_guests"]["title"] == "Guests" and panels["solar_guests"]["component_name"] == "lovelace"
    assert panels["solar_energy"]["component_name"] == "energy" and panels["solar_energy"]["url_path"] == "solar_energy"
    assert panels["solar_frigate"]["component_name"] == "iframe" and panels["solar_frigate"]["config"] == {"url": "/solar_frigate/ui/"}
    assert "Private" not in json.dumps(panels) and "dashboard-guest" not in json.dumps(panels)
    assert all(p["show_in_sidebar"] and p["default_visible"] and not p["require_admin"] for p in panels.values())


def test_a_published_panel_shows_however_the_owner_keeps_it_for_themselves():
    """The owner may have made the Energy dashboard (or a dashboard)
    administrators-only, so the viewer is told nothing about it, or taken it
    out of their own sidebar. The link publishes it all the same."""
    session, _ = _site_session()
    hidden = {"energy": {"component_name": "energy", "title": "energy", "url_path": "energy",
                         "require_admin": True, "show_in_sidebar": False, "default_visible": False}}
    for told in ({}, hidden):
        panels = session._panels(told)
        assert list(panels) == ["solar", "solar_guests", "solar_energy", "solar_frigate"]
        energy = panels["solar_energy"]
        assert energy["component_name"] == "energy" and energy["title"] == "energy" and energy["icon"] == "mdi:lightning-bolt"
        assert energy["show_in_sidebar"] is True and energy["default_visible"] is True and energy["require_admin"] is False
        assert panels["solar_guests"]["title"] == "Guests" and panels["solar_guests"]["show_in_sidebar"] is True


def test_each_dashboard_is_asked_for_by_its_public_address():
    session, wire = _site_session()
    session.handle({"id": 5, "type": "lovelace/config", "url_path": "solar_guests"})
    session.handle({"id": 6, "type": "lovelace/config", "url_path": "solar"})
    session.handle({"id": 7, "type": "lovelace/config", "url_path": "dashboard-private"})
    session.handle({"id": 8, "type": "lovelace/config", "url_path": "dashboard-guest"})
    assert [m["url_path"] for m in wire.forwarded] == ["dashboard-guest", "energy-public", "energy-public", "energy-public"]
    assert session._asked == {5: "dashboard-guest", 6: "energy-public", 7: "energy-public", 8: "energy-public"}
    session.handle({"id": 9, "type": "lovelace/dashboards/list"})
    assert [(d["url_path"], d["show_in_sidebar"]) for d in wire.answers[-1]["result"]] == [("solar", True), ("solar_guests", True)]


def test_an_edit_reaches_the_visitor_under_the_public_address_only():
    session, _ = _site_session()
    session._types[3] = "subscribe_events"

    def event(url_path):
        return session._filter_outbound({"id": 3, "type": "event", "event": {
            "event_type": "lovelace_updated", "data": {"url_path": url_path, "mode": "storage"}}})

    assert event("dashboard-guest")["event"]["data"]["url_path"] == "solar_guests"
    assert event("energy-public")["event"]["data"]["url_path"] == "solar"
    assert event("dashboard-private") is None


def test_the_startup_note_is_taken_quietly():
    session, wire = _session(_access())
    session.handle({"id": 4, "type": "subscribe_bootstrap_integrations"})
    assert not wire.forwarded and wire.answers == [{"id": 4, "type": "result", "success": True, "result": None}]
    session.handle({"id": 5, "type": "unsubscribe_events", "subscription": 4})
    assert not wire.forwarded, "and so is cancelling it"


# -- streams that run on are held to what is published when each piece is sent -----------------


def test_a_history_stream_leaves_out_what_is_no_longer_published():
    now = {"access": _access()}
    session, wire = _session(now["access"])
    session._access_now = lambda: now["access"]
    session.handle({"id": 1, "type": "history/stream", "entity_ids": ["light.porch", "sensor.pv", "lock.safe"]})
    assert wire.forwarded[0]["entity_ids"] == ["light.porch", "sensor.pv"]
    piece = lambda: {"id": 1, "type": "event", "event": {"states": {"light.porch": [{"s": "on"}], "sensor.pv": [{"s": "4"}]}, "start_time": 1, "end_time": 2}}
    assert set(session._filter_outbound(piece())["event"]["states"]) == {"light.porch", "sensor.pv"}
    # The porch light drops out of a list that picks its own entities.
    now["access"] = Access(entities={"sensor.pv"})
    out = session._filter_outbound(piece())
    assert out["event"]["states"] == {"sensor.pv": [{"s": "4"}]} and out["event"]["end_time"] == 2
    session._types[2] = "history/history_during_period"
    answer = session._filter_outbound({"id": 2, "type": "result", "success": True, "result": {"light.porch": [], "sensor.pv": []}})
    assert set(answer["result"]) == {"sensor.pv"}


def test_logbook_lines_are_held_to_what_is_published_now():
    session, _ = _session(Access(entities={"sensor.pv"}))
    session._types.update({1: "logbook/event_stream", 2: "logbook/get_events"})
    lines = [{"entity_id": "sensor.pv", "state": "4", "context_user_id": "u1"}, {"entity_id": "light.porch", "state": "on"},
             {"entity_id": None, "message": "x"}, {"name": "Home Assistant", "message": "started"}]
    out = session._filter_outbound({"id": 1, "type": "event", "event": {"events": [dict(l) for l in lines]}})
    assert out["event"]["events"] == [{"entity_id": "sensor.pv", "state": "4"}, {"name": "Home Assistant", "message": "started"}]
    out = session._filter_outbound({"id": 2, "type": "result", "success": True, "result": [dict(l) for l in lines]})
    assert [l.get("entity_id") for l in out["result"]] == ["sensor.pv", None]


@pytest.mark.parametrize("kind, extra", [
    ("weather/subscribe_forecast", {"forecast_type": "daily"}),
    ("calendar/event/subscribe", {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}),
])
def test_a_forecast_or_calendar_stops_when_its_entity_is_no_longer_published(kind, extra):
    now = {"access": Access(entities={"weather.home", "calendar.bins"})}
    session, wire = _session(now["access"])
    session._access_now = lambda: now["access"]
    entity = "weather.home" if kind.startswith("weather") else "calendar.bins"
    session.handle({"id": 3, "type": kind, "entity_id": entity, **extra})
    wire.subscriptions[3] = lambda: wire.ended.append(3)
    event = {"id": 3, "type": "event", "event": {"forecast": [], "events": []}}
    assert session._filter_outbound(dict(event)) is not None and not wire.ended
    now["access"] = Access(entities=set())
    assert session._filter_outbound(dict(event)) is None and wire.ended == [3]


# -- templates the owner wrote with entity tokens (Dashboard References) ------------------------

TOKENED = ("{% set w = '[[entity:weather]]' %}{% macro deg(x) %}{{ x | round }}°{% endmacro %}"
           "{{ states(w) | title }} {{ deg(state_attr('[[entity:weather]]', 'temperature')) }} at {{ area_name('[[entity:porch]]') }}")


def _tokened():
    access = _access()
    access.entities |= {"weather.home"}
    access.strings |= {TOKENED}
    return access


def test_the_owners_template_with_its_tokens_filled_in_is_rendered_under_watch():
    """It uses a macro and area_name, which no template a card merely
    assembled may; but every word of it is the owner's, and the page only
    said which published entities stand where the tokens were."""
    filled = TOKENED.replace("[[entity:weather]]", "weather.home").replace("[[entity:porch]]", "light.porch")
    session, wire = _ask(_tokened(), type="render_template", template=filled, timeout=10, report_errors=True)
    assert wire.forwarded and wire.forwarded[0]["timeout"] == 5 and wire.forwarded[0]["report_errors"] is False
    assert 7 in session._watched


@pytest.mark.parametrize("change", [
    lambda t: t.replace("[[entity:weather]]", "lock.safe").replace("[[entity:porch]]", "light.porch"),      # not published
    lambda t: t.replace("[[entity:weather]]", "weather.home", 1).replace("[[entity:weather]]", "sensor.pv").replace("[[entity:porch]]", "light.porch"),  # one token, two entities
    lambda t: t.replace("[[entity:weather]]", "weather.home").replace("[[entity:porch]]", "light.porch") + "{{ areas() }}",   # something added
    lambda t: t.replace("[[entity:weather]]", "weather.home').state ~ states('lock.safe").replace("[[entity:porch]]", "light.porch"),  # more than an entity in a token's place
    lambda t: t.replace("[[entity:weather]]", "weather.home").replace("[[entity:porch]]", "light.porch").replace("title", "upper"),  # a word changed
])
def test_anything_but_published_entities_in_the_tokens_places_is_not_that(change):
    session, wire = _ask(_tokened(), type="render_template", template=change(TOKENED))
    assert _refused(wire) and not session._watched
