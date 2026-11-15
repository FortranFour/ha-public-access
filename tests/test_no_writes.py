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
    media = text[text.index("async def _serve_media"):text.index("async def _serve_page")]
    assert media.index("access.path_allowed(path)") < media.index("async_sign_path(")
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
        assert text.count("async_sign_path(") == (1 if name == "view.py" else 0), name


def test_the_mirror_filters_what_home_assistant_answers():
    """States, the entity registry, history and statistics are cut down to what
    the published view uses before they reach the visitor."""
    text = (PACKAGE / "mirror.py").read_text(encoding="utf-8")
    assert 'self._entity_allowed(s.get("entity_id"))' in text        # get_states
    assert 'self._entity_allowed(e.get("entity_id"))' in text        # entity registry
    assert "if s in self._statistics" in text                        # statistics
    assert '"entity_ids": [e for e in wanted if self._entity_allowed(e)]' in text  # history
