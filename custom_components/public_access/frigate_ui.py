"""Frigate's own interface for a visitor: to look, never to change.

How it is kept read-only and to the chosen cameras
---------------------------------------------------
Frigate (0.17 and later) has users and roles of its own, and a role can be
limited to some cameras. Frigate then enforces that everywhere: lists,
pictures, recordings, live streams, its socket. That is the enforcement used
here, rather than a second copy of Frigate's rules that every Frigate update
could leave behind:

* The owner makes a Frigate user for visitors, with a role limited to the
  cameras to show, and gives its name and password to this link.
* This module signs in as that user on Frigate's *authenticated* port (8971;
  the port the Frigate integration uses, 5000, treats everyone as
  administrator and is never used here) and fetches for the visitor.
* The visitor never holds Frigate's cookie: it stays in this process.

On top of what Frigate enforces, nothing but reading is passed on:

* Only GET. Signing in or out, users, the configuration as written, logs,
  exports, restarting are not passed on at all (policy.frigate_ui_path).
* Frigate's socket is sent requests for state only. Frigate would let the
  user move a camera (PTZ); a visitor does not (policy.frigate_socket_message).
* A live stream is asked only to be watched: no microphone
  (policy.stream_request, the same rule as for camera cards).
* Frigate's configuration and statistics are cut down to the allowed cameras
  and stripped of every address (policy.public_frigate_config).

And the link checks Frigate's side before showing anything: the account must
not be an administrator, and every camera it may see must be one the link
publishes ("Frigate cameras visitors may browse", or a camera on a published
view). Otherwise the panel shows nothing and the owner's diagnostics say why.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    CookieJar,
    TCPConnector,
    WSMsgType,
    web,
)

from homeassistant.core import HomeAssistant

from . import guard
from .const import (
    CONF_FRIGATE_UI_PASSWORD,
    CONF_FRIGATE_UI_URL,
    CONF_FRIGATE_UI_USER,
)
from .policy import (
    clean_stream_info,
    frigate_page_guard,
    frigate_socket_message,
    frigate_ui_hidden_page,
    frigate_ui_page_like,
    frigate_ui_path,
    public_frigate_config,
    public_frigate_stats,
    stream_request,
)

_LOGGER = logging.getLogger(__name__)

CHECK_SECONDS = 60
RECHECK_SECONDS = 5
# What a browser may be told about a fetched file, and what it may ask with.
_ANSWER_HEADERS = (
    "Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag",
    "Last-Modified", "Cache-Control", "Content-Disposition", "Content-Encoding",
)
_ASK_HEADERS = ("Range", "If-None-Match", "If-Modified-Since", "Accept", "Accept-Language")
_CSP = (
    "default-src 'self' blob: data:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "media-src 'self' blob: data:; connect-src 'self' blob: data:; "
    "worker-src 'self' blob:; font-src 'self' data:; "
    "form-action 'none'; base-uri 'self'; frame-ancestors 'self'"
)
_PLAIN = {"Accept-Encoding": "identity"}
_SOCKETS = {
    "ws": "state",
    "live/mse/api/ws": "stream",
    "live/webrtc/api/ws": "stream",
}


def default_url(hass: HomeAssistant) -> str:
    """A first guess at Frigate's authenticated address: the host the Frigate
    integration talks to, on port 8971."""
    from urllib.parse import urlsplit

    try:
        for entry in hass.config_entries.async_entries("frigate"):
            host = urlsplit(str(entry.data.get("url") or "")).hostname
            if host:
                return f"https://{host}:8971"
    except Exception:  # noqa: BLE001
        pass
    return "https://frigate:8971"


class FrigateUi:
    """One link's way into Frigate, as the visitors' Frigate user."""

    def __init__(self, hass: HomeAssistant, url: str, user: str, password: str) -> None:
        self.hass = hass
        self.base = url.rstrip("/")
        self._user = user
        self._password = password
        self._session: ClientSession | None = None
        self._signed_in = False
        self._checked_at = 0.0
        self._cameras: list[str] | None = None
        self.problem: str | None = None
        self._lock = asyncio.Lock()

    def session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            # Frigate's own certificate is self-signed; the address is the
            # owner's. Cookies are kept for an address that may be an IP.
            # Answers are passed on byte for byte, compressed as Frigate sent
            # them, with the headers that say so: never unpacked here and
            # then labelled as packed. What is read here asks for plain text.
            self._session = ClientSession(
                cookie_jar=CookieJar(unsafe=True),
                connector=TCPConnector(ssl=False),
                auto_decompress=False,
            )
            self._signed_in = False
        return self._session

    async def async_close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def async_sign_in(self) -> str | None:
        """Sign in. Returns an error key, or None."""
        try:
            async with self.session().post(
                f"{self.base}/api/login",
                json={"user": self._user, "password": self._password},
                headers=_PLAIN,
                timeout=ClientTimeout(total=15),
                allow_redirects=False,
            ) as response:
                if response.status in (400, 401, 403):
                    return "invalid_auth"
                if response.status != 200:
                    return "cannot_connect"
        except (ClientError, OSError, asyncio.TimeoutError):
            return "cannot_connect"
        self._signed_in = True
        return None

    async def async_profile(self) -> tuple[str | None, dict[str, Any] | None]:
        """(error key, Frigate's description of the account)."""
        if not self._signed_in:
            error = await self.async_sign_in()
            if error:
                return error, None
        try:
            async with self.session().get(
                f"{self.base}/api/profile", headers=_PLAIN, timeout=ClientTimeout(total=15),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    self._signed_in = False
                    return "invalid_auth", None
                if response.status != 200:
                    return "cannot_connect", None
                return None, await response.json(content_type=None)
        except (ClientError, OSError, asyncio.TimeoutError, ValueError):
            return "cannot_connect", None

    async def async_cameras(self, published: set[str]) -> list[str] | None:
        """The cameras the visitors' interface shows, or None when it must
        show nothing (``problem`` then says why, for the owner)."""
        async with self._lock:
            # While there is a problem, look again soon: the owner is
            # probably mending it and waiting for the panel to come back.
            wait = CHECK_SECONDS if self._cameras is not None else RECHECK_SECONDS
            if self._checked_at and time.monotonic() - self._checked_at < wait:
                return self._cameras
            error, profile = await self.async_profile()
            if error == "invalid_auth" and profile is None:
                # A session that ran out: once more from the start.
                error, profile = await self.async_profile()
            self._checked_at = time.monotonic()
            self._cameras = None
            if error or not isinstance(profile, dict):
                self.problem = {
                    "invalid_auth": "Frigate did not accept the visitors' user name and password.",
                    "cannot_connect": f"Frigate could not be reached at {self.base}.",
                }.get(error or "", "Frigate did not describe the visitors' account.")
                return None
            if profile.get("role") == "admin":
                self.problem = "The Frigate account given for visitors is an administrator. Use one with a role limited to the cameras to show."
                return None
            allowed = [str(c) for c in profile.get("allowed_cameras") or []]
            beyond = sorted(set(allowed) - published)
            if beyond:
                self.problem = (
                    "The Frigate account for visitors can see cameras this link does not publish: "
                    + ", ".join(beyond)
                    + ". Limit its role in Frigate, or tick those cameras under 'Frigate cameras visitors may browse'."
                )
                return None
            if not allowed:
                self.problem = "The Frigate account for visitors may see no camera."
                return None
            self.problem = None
            self._cameras = allowed
            return allowed


def for_link(coordinator: Any) -> FrigateUi | None:
    """The link's FrigateUi, made on first use and remade when its settings change."""
    options = coordinator.options
    url = str(options.get(CONF_FRIGATE_UI_URL) or "").strip()
    user = str(options.get(CONF_FRIGATE_UI_USER) or "").strip()
    password = str(options.get(CONF_FRIGATE_UI_PASSWORD) or "")
    if not (url and user and password):
        return None
    current = getattr(coordinator, "frigate_ui", None)
    if current is None:
        current = coordinator.frigate_ui = FrigateUi(coordinator.hass, url, user, password)
    return current


def tell_owner(coordinator: Any, problem: str | None) -> None:
    """A notification in Home Assistant while the link's Frigate panel shows
    nothing, saying why; taken away when it shows again. The visitor is told
    nothing of it."""
    from homeassistant.components import persistent_notification

    if getattr(coordinator, "frigate_told", None) == problem:
        return
    coordinator.frigate_told = problem
    notice = f"public_access_frigate_{coordinator.entry.entry_id}"
    if problem is None:
        persistent_notification.async_dismiss(coordinator.hass, notice)
        return
    persistent_notification.async_create(
        coordinator.hass,
        f"The Frigate panel of the public link **{coordinator.entry.title}** shows nothing: {problem}",
        title="Public Access: Frigate is not shown",
        notification_id=notice,
    )


async def async_check(
    hass: HomeAssistant, url: str, user: str, password: str
) -> tuple[str | None, list[str]]:
    """For the settings form: can this account be used? (an error key or
    None, the cameras the account may see)."""
    if not url.startswith(("http://", "https://")):
        return "frigate_cannot_connect", []
    probe = FrigateUi(hass, url, user, password)
    try:
        error, profile = await probe.async_profile()
        if error:
            return "frigate_" + error, []
        if not isinstance(profile, dict) or profile.get("role") == "admin":
            return "frigate_is_admin", []
        return None, [str(c) for c in profile.get("allowed_cameras") or []]
    finally:
        await probe.async_close()


def _not_found() -> web.Response:
    response = web.Response(text="404: Not Found", status=404)
    response.headers["Cache-Control"] = "no-store"
    return response


async def async_serve(view: Any, request: web.Request, panel: Any, extra: str) -> web.StreamResponse:
    """One request of a visitor's Frigate page."""
    coordinator = view._coordinator  # noqa: SLF001 - the view's own link
    ui = for_link(coordinator)
    if ui is None:
        return _not_found()
    rest = extra[2:] if extra.startswith("ui") else extra
    if rest == "" and not request.path.endswith("/"):
        # Frigate's pages are written relative to a folder.
        raise web.HTTPFound(f"/{panel.path}/ui/")
    if rest.strip("/") == "api/exports":
        # Frigate's interface asks for the list of exports on its way in.
        # A visitor has none: an empty list, so that it does not report an
        # error, and nothing is asked of Frigate.
        return web.json_response([], headers={"Cache-Control": "no-store"})
    if frigate_ui_hidden_page(rest):
        # Its settings, and the pages Frigate keeps for administrators: the
        # live view instead.
        raise web.HTTPFound(f"/{panel.path}/ui/")
    path = frigate_ui_path(rest)
    if path is None:
        coordinator.note_refusal(f"Frigate interface: not passed on for a visitor: {rest[:120]}")
        return _not_found()
    access = await coordinator.async_access()
    published = set(access.frigate.cameras) if access.frigate else set()
    cameras = await ui.async_cameras(published)
    tell_owner(coordinator, None if cameras is not None else ui.problem)
    if cameras is None:
        if ui.problem:
            coordinator.note_refusal("Frigate interface is not shown: " + ui.problem)
        return web.Response(
            text="Frigate is not available here.", status=503, headers={"Cache-Control": "no-store"}
        )
    base_path = f"/{panel.path}/ui"
    if request.headers.get("Upgrade", "").lower() == "websocket":
        kind = _SOCKETS.get(path) or ("video" if path.startswith("live/jsmpeg/") else None)
        if kind is None:
            return _not_found()
        return await _serve_socket(view, request, ui, path, kind)

    headers = {name: request.headers[name] for name in _ASK_HEADERS if name in request.headers}
    headers["X-Ingress-Path"] = base_path
    cleaner = _cleaner(path, cameras)
    # Frigate's page itself gets something put into it (policy.frigate_page_guard),
    # so it is asked for whole, never "not changed since".
    page = frigate_ui_page_like(path)
    if page:
        headers.pop("If-None-Match", None)
        headers.pop("If-Modified-Since", None)
        headers.pop("Range", None)
    # What is changed here is asked for plain; everything else in whatever
    # packing the visitor's browser accepts, and passed on as it comes.
    headers["Accept-Encoding"] = (
        "identity" if cleaner is not None or page else request.headers.get("Accept-Encoding", "identity")
    )
    url = f"{ui.base}/{path}"
    if request.query_string:
        url += "?" + request.query_string
    for attempt in (1, 2):
        try:
            upstream = await ui.session().get(
                url, headers=headers, timeout=ClientTimeout(total=None, sock_connect=10), allow_redirects=False
            )
        except (ClientError, OSError, asyncio.TimeoutError):
            return web.Response(text="Frigate is not available here.", status=502)
        if upstream.status == 401 and attempt == 1:
            upstream.release()
            if await ui.async_sign_in():
                return web.Response(text="Frigate is not available here.", status=502)
            continue
        break
    try:
        if upstream.status in (301, 302, 303, 307, 308):
            # Frigate sends an unknown visitor to its sign-in page: not here.
            return _not_found()
        kept = {name: upstream.headers[name] for name in _ANSWER_HEADERS if name in upstream.headers}
        kept["Content-Security-Policy"] = _CSP
        kept["Referrer-Policy"] = "no-referrer"
        kept["X-Robots-Tag"] = "noindex, nofollow"
        kept["Permissions-Policy"] = "microphone=(), camera=(), display-capture=()"
        if cleaner is not None and upstream.status == 200:
            try:
                body = json.dumps(cleaner(await upstream.json(content_type=None)))
            except ValueError:
                return _not_found()
            return web.Response(
                text=body,
                content_type="application/json",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": _CSP,
                    "X-Robots-Tag": "noindex, nofollow",
                },
            )
        if page and upstream.headers.get("Content-Type", "").lower().startswith("text/html"):
            if upstream.status != 200 or upstream.headers.get("Content-Encoding", "identity").lower() != "identity":
                # Not a page that can be read here, so not one to hand on as it is.
                return _not_found()
            html = (await upstream.read()).decode("utf-8", "replace")
            guard_html = frigate_page_guard(base_path)
            at = html.lower().find("<head>")
            html = html[: at + 6] + guard_html + html[at + 6 :] if at >= 0 else guard_html + html
            return web.Response(
                text=html,
                content_type="text/html",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": _CSP,
                    "Referrer-Policy": "no-referrer",
                    "X-Robots-Tag": "noindex, nofollow",
                    "Permissions-Policy": "microphone=(), camera=(), display-capture=()",
                },
            )
        response = web.StreamResponse(status=upstream.status, headers=kept)
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        upstream.release()


def _cleaner(path: str, cameras: list[str]) -> Any:
    """Answers that are cut down before a visitor gets them."""
    if path == "api/config":
        return lambda data: public_frigate_config(data, cameras)
    if path == "api/stats" or path.startswith("api/stats/"):
        return lambda data: public_frigate_stats(data, cameras)
    if path.startswith("api/go2rtc/"):
        return clean_stream_info
    return None


async def _serve_socket(
    view: Any, request: web.Request, ui: FrigateUi, path: str, kind: str
) -> web.StreamResponse:
    """One of Frigate's sockets, with only reading passed on from the visitor."""
    coordinator = view._coordinator  # noqa: SLF001
    visitor = guard.visitor_address(request)
    if not view._streams.acquire(visitor):  # noqa: SLF001
        return web.Response(text="503: Too many streams", status=503, headers={"Retry-After": "30"})
    upstream = None
    try:
        url = ui.base.replace("http", "ws", 1) + "/" + path
        if request.query_string:
            url += "?" + request.query_string
        try:
            upstream = await ui.session().ws_connect(url, timeout=10, heartbeat=30, max_msg_size=0)
        except (ClientError, OSError, asyncio.TimeoutError):
            return web.Response(text="Frigate is not available here.", status=502)
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
        await socket.prepare(request)
        coordinator.sockets.add(socket)
        check = {"state": frigate_socket_message, "stream": stream_request}.get(kind)

        async def from_visitor() -> None:
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    break
                passed = check(message.data) if check else None
                if passed is not None:
                    await upstream.send_str(passed)

        async def to_visitor() -> None:
            async for message in upstream:
                if message.type == WSMsgType.BINARY:
                    await socket.send_bytes(message.data)
                elif message.type == WSMsgType.TEXT:
                    await socket.send_str(message.data)
                else:
                    break

        tasks = [asyncio.ensure_future(from_visitor()), asyncio.ensure_future(to_visitor())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            coordinator.sockets.discard(socket)
            await socket.close()
        return socket
    finally:
        if upstream is not None:
            await upstream.close()
        view._streams.release(visitor)  # noqa: SLF001
