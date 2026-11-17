"""Static guarantee: this integration can only read Home Assistant.

A public, unauthenticated endpoint must not be able to reach a write path, so the
package is scanned for any call that could mutate state. This is a blunt instrument
on purpose: it fails on the mere presence of such a call, before review.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "public_access"

FORBIDDEN = (
    "async_call(",         # service calls
    "services.call",
    # Not "call_service" bare: that is the websocket message *name*, which
    # mirror.py must mention in order to refuse it.
    "async_set(",          # state writes ("async_setup" must not match)
    "states.async_set",
    "async_save(",         # store / config writes
    "async_update_entry_data",
    "async_create_system_user(",  # auth store writes: allowed only in mirror.py
    "async_create_refresh_token(",
    "async_remove_user(",
    "async_update_user(",
    "save_prefs",
    "import_statistics",
    "clear_statistics",
    "adjust_sum_statistics",
    "config/save",
    "subprocess",
    "os.system",
    "eval(",
    "exec(",
    "exec_module(",       # running code loaded at runtime: never
)

# The writes this integration does perform, each to its own state and each
# named here so a new one cannot appear unnoticed:
# - mirror.py remembers the id of the read-only viewer user it created, and
#   creating that user (and its refresh token) is itself a write to the auth
#   store — once, and never anything that touches the owner's own users.
ALLOWED = {
    ("mirror.py", "async_save("),
    ("mirror.py", "async_create_system_user("),
    ("mirror.py", "async_create_refresh_token("),
}


def python_sources():
    return sorted(PACKAGE.rglob("*.py"))


def test_package_contains_no_write_paths():
    offenders = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            code = line.split("#", 1)[0]
            for needle in FORBIDDEN:
                if needle in code and (path.name, needle) not in ALLOWED:
                    offenders.append(f"{path.name}:{line_number}: {needle} -> {line.strip()}")
    assert not offenders, "write-capable calls found:\n" + "\n".join(offenders)


def test_only_get_is_implemented_on_the_public_view():
    text = (PACKAGE / "view.py").read_text(encoding="utf-8")
    for verb in ("async def post", "async def put", "async def delete", "async def patch"):
        assert verb not in text, f"{verb} must not exist on the public view"
    assert "async def get" in text


def test_public_view_requires_no_auth_by_design_and_says_so():
    text = (PACKAGE / "view.py").read_text(encoding="utf-8")
    assert "requires_auth = False" in text


def test_the_only_visitor_parameters_are_checked_against_what_is_published():
    """The page itself reads nothing from the query string. Two endpoints do:
    history, which cuts the entity list down to the published views, and the
    media endpoint, which serves a path (query included) only if the policy
    allows it and otherwise answers that it does not exist."""
    text = (PACKAGE / "view.py").read_text(encoding="utf-8")
    assert text.count("request.query") == 3
    assert "history.parse_query" in text and text.count("dict(request.query)") == 1
    assert text.count("request.query_string") == 2
    media = text[text.index("async def _serve_media"):text.index("async def _serve_stream")]
    # Three things are signed there, each after its own check: a stream's
    # description and LLM Vision's timeline, both fetched here and passed on
    # cleaned, never handed over as a link, and a path the policy allows.
    # A live stream's socket is handed to _serve_stream before any of them.
    assert media.index("access.stream_socket_allowed(path)") < media.index("access.stream_info_allowed(path)")
    stream, timeline, link = [m.start() for m in __import__("re").finditer(r"async_sign_path\(\n", media)]
    assert media.index("access.stream_info_allowed(path)") < stream < media.index("clean_stream_info(info)")
    assert media.index("access.llmvision_events_allowed(path)") < timeline < media.index("access.llmvision_events(timeline)")
    assert media.index("access.path_allowed(path)") < link
    history_module = (PACKAGE / "history.py").read_text(encoding="utf-8")
    assert "[e for e in wanted if e in allowed]" in history_module


def test_nothing_is_signed_without_asking_the_policy():
    """A signed path is a key. The mirror signs for a visitor in exactly two
    places, and both ask policy.Access.path_allowed first."""
    mirror = (PACKAGE / "mirror.py").read_text(encoding="utf-8")
    branch = mirror[mirror.index('elif kind == "auth/sign_path":'):]
    assert branch.index("self._access.path_allowed(") < branch.index("self._refuse(")
    for name in ("view.py", "mirror.py", "coordinator.py", "data.py", "mirror_core.py"):
        text = (PACKAGE / name).read_text(encoding="utf-8")
        assert text.count("async_sign_path(\n") == (4 if name == "view.py" else 0), name


def test_a_visitor_cannot_talk_through_a_camera():
    """A live stream's socket would take a microphone. No link to it is ever
    signed for a visitor; the public endpoint opens it, as the viewer, with a
    link that never leaves the server, and passes on from the visitor only
    what policy.stream_request lets through. The page is told to offer no
    microphone at all."""
    view = (PACKAGE / "view.py").read_text(encoding="utf-8")
    stream = view[view.index("async def _serve_stream"):view.index("async def _serve_page")]
    assert stream.count("upstream.send_") == 1 and "await upstream.send_str(passed)" in stream
    assert stream.index("passed = stream_request(message.data)") < stream.index("upstream.send_str(passed)")
    assert "signed" not in stream[stream.index("socket = web.WebSocketResponse"):], "the link stays here"
    assert 'response.headers["Permissions-Policy"] = "microphone=(), camera=(), display-capture=()"' in view
    policy = (PACKAGE / "policy.py").read_text(encoding="utf-8")
    frigate = policy[policy.index("def _frigate_path_allowed"):policy.index("def stream_socket_allowed")]
    assert '"mse"' not in frigate and '"webrtc"' not in frigate
    mirror = (PACKAGE / "mirror.py").read_text(encoding="utf-8")
    branch = mirror[mirror.index('elif kind == "auth/sign_path":'):]
    assert branch.index("self._access.stream_socket_allowed(path)") < branch.index("self._access.path_allowed(")
    assert 'offer = listen_only_sdp(msg.get("offer"))' in mirror


def test_templates_are_rendered_only_written_or_watched():
    """render_template reads states. It is forwarded for a template written in
    a published view, or with its results checked against what it read."""
    mirror = (PACKAGE / "mirror.py").read_text(encoding="utf-8")
    branch = mirror[mirror.index('elif kind == "render_template":'):mirror.index("self._types[msg_id] = kind")]
    assert "self._access.template_verdict(" in branch and "self._watched[msg_id]" in branch
    assert "if msg_id in self._watched and message.get(\"type\") == \"event\":" in mirror


def test_the_mirror_filters_what_home_assistant_answers():
    """States, the entity registry, history and statistics are cut down to what
    the published view uses before they reach the visitor."""
    text = (PACKAGE / "mirror.py").read_text(encoding="utf-8")
    assert 'self._entity_allowed(s.get("entity_id"))' in text        # get_states
    assert 'self._entity_allowed(e.get("entity_id"))' in text        # entity registry
    assert "if s in self._statistics" in text                        # statistics
    assert '"entity_ids": [e for e in wanted if self._entity_allowed(e)]' in text  # history


def test_frigates_interface_is_fetched_never_written_to():
    """The public Frigate panel signs in to Frigate as the visitors' account
    (the one POST there is, with the owner's settings), and from then on only
    fetches: a visitor's request reaches Frigate as a GET of a path
    policy.frigate_ui_path let through, or as a socket whose messages from
    the visitor pass a filter. The view has no handler but GET."""
    text = (PACKAGE / "frigate_ui.py").read_text(encoding="utf-8")
    assert text.count(".post(") == 1 and '/api/login' in text[text.index(".post("):text.index(".post(") + 200]
    assert ".put(" not in text and ".delete(" not in text and ".patch(" not in text and ".request(" not in text
    serve = text[text.index("async def async_serve"):text.index("def _cleaner")]
    assert serve.index("path = frigate_ui_path(rest)") < serve.index("ui.session().get(")
    assert serve.index("cameras = await ui.async_cameras(published)") < serve.index("ui.session().get(")
    socket = text[text.index("async def _serve_socket"):]
    assert socket.count("upstream.send_") == 1 and "passed = check(message.data) if check else None" in socket
    assert "Set-Cookie" not in text.replace('"""', "")[text.index("_ANSWER_HEADERS = ("):text.index("_ASK_HEADERS")]
    view = (PACKAGE / "view.py").read_text(encoding="utf-8")
    assert "async def post(" not in view and "async def put(" not in view and "async def delete(" not in view


def test_frigates_answers_are_passed_on_as_they_come():
    """Frigate packs its pages (gzip). What is fetched for a visitor is
    passed on byte for byte with the header that says how it is packed; it is
    never unpacked here and still labelled as packed, which a browser shows
    as an empty page. What is read here is asked for plain."""
    text = (PACKAGE / "frigate_ui.py").read_text(encoding="utf-8")
    assert "auto_decompress=False" in text and '_PLAIN = {"Accept-Encoding": "identity"}' in text
    assert text.count("headers=_PLAIN") == 2, "signing in and the account check"
    serve = text[text.index("async def async_serve"):text.index("def _cleaner")]
    assert '"identity" if cleaner is not None or page else request.headers.get("Accept-Encoding", "identity")' in serve


def test_frigates_page_is_never_handed_on_without_the_guard():
    """Frigate's own page offers settings and an account menu. It reaches a
    visitor with the guard in it (policy.frigate_page_guard) or not at all,
    and is asked for whole, so a copy from before cannot be reused."""
    text = (PACKAGE / "frigate_ui.py").read_text(encoding="utf-8")
    serve = text[text.index("async def async_serve"):text.index("def _cleaner")]
    assert serve.index("if frigate_ui_hidden_page(rest):") < serve.index("path = frigate_ui_path(rest)")
    html = serve[serve.index('startswith("text/html")'):serve.index("response = web.StreamResponse(")]
    assert "return _not_found()" in html and "frigate_page_guard(base_path)" in html and '"Cache-Control": "no-store"' in html
    for header in ("If-None-Match", "If-Modified-Since"):
        assert f'headers.pop("{header}", None)' in serve


def test_an_id_is_checked_before_anything_is_noted_about_its_message():
    """The kind of a request decides how its answers are filtered. A second
    message under a used id must be turned down before it can change that."""
    mirror = (PACKAGE / "mirror.py").read_text(encoding="utf-8")
    handle = mirror[mirror.index("    def handle(self, msg"):mirror.index("    def close(self)")]
    check = handle.index("if msg_id <= self._last_id:")
    for noted in ("self._types[msg_id] = kind", "self._watched[msg_id] =", "self._asked[msg_id] =",
                  "self._conditions.add(msg_id)", "silent_subscription(msg_id)"):
        assert check < handle.index(noted), noted
    assert handle.count("self._types[msg_id] = kind") == 1
    outbound = mirror[mirror.index("    def _filter_outbound"):mirror.index("    def _watched_template_event")]
    assert outbound.index("if kind is None:") < outbound.index('message.get("success") is False')
