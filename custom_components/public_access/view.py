"""The public, unauthenticated HTTP surface.

Design rules enforced here:

* Only GET is implemented, so every other verb gets an automatic 405.
* What a visitor may read is decided server-side from the published views. The
  two places a visitor names something themselves — history and the pictures
  and clips fetched under `api/` — are both cut down to what is published
  (history.parse_query, policy.Access.path_allowed).
* Nothing is served unless the integration is enabled.
* `X-Frame-Options: SAMEORIGIN` is applied by Home Assistant's own middleware after
  this handler returns and cannot be overridden from here, so embedding the page in
  another site requires a header rewrite at the reverse proxy. CSP, X-Robots-Tag and
  Cache-Control do pass through and are set below.
"""

from __future__ import annotations

from datetime import timedelta
import asyncio
import hashlib
from homeassistant.helpers.json import json_dumps
import logging
import time
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import assets, data as ha_data, guard
from .const import (
    CONF_CACHE_SECONDS,
    CONF_ENABLED,
    CONF_FRAME_ANCESTORS,
    CONF_LOADING_BACKGROUND,
    CONF_NOINDEX,
    DEFAULT_CACHE_SECONDS,
    MAX_PUBLIC_SESSIONS,
    MAX_PUBLIC_STREAMS,
    MAX_SESSIONS_PER_CLIENT,
    MAX_STREAMS_PER_CLIENT,
    MEDIA_RATE_LIMIT_PER_MINUTE,
    RATE_LIMIT_PER_MINUTE,
)
from .coordinator import PublicDashboardCoordinator
from .policy import MAX_STREAM_REQUEST, clean_stream_info, stream_request

_LOGGER = logging.getLogger(__name__)


class RateLimiter:
    """Fixed-window per-IP limiter. Small and good enough for a public page."""

    def __init__(self, per_minute: int = RATE_LIMIT_PER_MINUTE) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, tuple[int, int]] = {}

    def allow(self, client: str | None) -> bool:
        key = client or "unknown"
        window = int(time.time() // 60)
        start, count = self._hits.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        self._hits[key] = (start, count)
        if len(self._hits) > 4096:  # crude but bounded
            self._hits = {
                k: v for k, v in self._hits.items() if v[0] == window
            }
        return count <= self._per_minute


class PublicDashboardView(HomeAssistantView):
    """Serves one dashboard publicly, read-only."""

    requires_auth = False
    name = "public_access:dashboard"

    def __init__(
        self,
        hass: HomeAssistant,
        public_path: str,
        coordinator: PublicDashboardCoordinator,
    ) -> None:
        self.hass = hass
        self.public_path = public_path
        self.url = f"/{public_path}"
        self.extra_urls = [f"/{public_path}/{{extra:.+}}"]
        self._coordinator = coordinator
        self._limiter = RateLimiter()
        self._media_limiter = RateLimiter(MEDIA_RATE_LIMIT_PER_MINUTE)
        self._sessions = guard.SessionLimiter(MAX_PUBLIC_SESSIONS, MAX_SESSIONS_PER_CLIENT)
        self._streams = guard.SessionLimiter(MAX_PUBLIC_STREAMS, MAX_STREAMS_PER_CLIENT)
        self.last_visit: dict[str, Any] | None = None

    def rebind(self, coordinator: PublicDashboardCoordinator) -> None:
        """Point this route at a new config entry's coordinator.

        aiohttp cannot unregister a route, so when the integration is removed and
        added again without a restart the original view object is still the one
        serving this path. Without rebinding, the path would answer 404 forever
        even though the integration is configured — and removing and re-adding is
        the first thing anyone tries when something looks wrong.
        """
        self._coordinator = coordinator

    # -- helpers ---------------------------------------------------------------

    @property
    def _options(self) -> dict[str, Any]:
        return self._coordinator.options

    def _decorate(self, response: web.Response) -> web.Response:
        options = self._options
        max_age = int(options.get(CONF_CACHE_SECONDS, DEFAULT_CACHE_SECONDS))
        response.headers["Cache-Control"] = f"public, max-age={max_age}"
        if options.get(CONF_NOINDEX, True):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        ancestors = options.get(CONF_FRAME_ANCESTORS) or "'self'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            f"frame-ancestors {ancestors}"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _json(self, payload: Any, status: int = 200) -> web.Response:
        # Home Assistant's own encoder: State objects become their dict form, as
        # the frontend and cards expect from the real history endpoint.
        body = json_dumps(payload)
        response = web.Response(
            body=body.encode(), content_type="application/json", status=status
        )
        response.headers["ETag"] = hashlib.sha256(body.encode()).hexdigest()[:32]
        return self._decorate(response)

    def _unavailable(self, message: str) -> web.Response:
        html = assets.get("unavailable.html").replace("{{message}}", message)
        response = web.Response(text=html, content_type="text/html", status=503)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _note_visit(self, request: web.Request, visitor: str | None) -> None:
        """Note how the visitor was told apart, for diagnostics; no action needed."""
        self.last_visit = {
            "source": guard.visitor_source(request),
            "home_assistant_resolved_proxy": guard.proxy_not_trusted(request) is None,
        }
        _LOGGER.debug(
            "Public visit from %s (remote %s, X-Forwarded-For %s)",
            visitor,
            request.remote,
            request.headers.get("X-Forwarded-For"),
        )

    # -- routing ---------------------------------------------------------------

    async def get(self, request: web.Request, extra: str = "") -> web.Response:
        """Dispatch the public GET surface."""
        if not self._coordinator.active or not self._options.get(CONF_ENABLED, True):
            # Deliberately indistinguishable from a path that was never configured.
            return web.Response(text="404: Not Found", status=404)

        visitor = guard.visitor_address(request)
        self._note_visit(request, visitor)

        route = extra.strip("/")
        media = route.startswith("api/") and not route.startswith("api/history/")
        if not (self._media_limiter if media else self._limiter).allow(visitor):
            response = web.Response(text="429: Too Many Requests", status=429)
            response.headers["Retry-After"] = "60"
            return response

        # Mirror is the only way a dashboard is published. The live renderer and
        # the snapshot companion were removed in 0.4: entries still set to them
        # are served as mirror.
        return await self._mirror(request, route, visitor)

    async def _mirror(
        self, request: web.Request, route: str, visitor: str | None
    ) -> web.StreamResponse:
        """Home Assistant's real frontend over a read-only websocket proxy.

        Routes: the page itself (any sub-path, since the frontend routes views
        client-side), `ws`, the websocket the page is steered to, and `api/...`,
        where the page's REST requests are steered.
        """
        if route == "ws":
            if not self._sessions.acquire(visitor):
                response = web.Response(text="503: Too many visitors", status=503)
                response.headers["Retry-After"] = "30"
                return response
            try:
                return await self._serve_ws(request)
            finally:
                self._sessions.release(visitor)
        return await self._serve_page(request, route)

    async def _serve_ws(self, request: web.Request) -> web.StreamResponse:
        from . import mirror

        coordinator = self._coordinator
        return await mirror.async_serve_websocket(
            self.hass,
            request,
            public_path=self.public_path,
            dashboard=coordinator.dashboard,
            access=await coordinator.async_access(),
            publish=coordinator.publish,
            title=coordinator.public_title,
            notes=coordinator,
        )

    async def _serve_media(self, request: web.Request, route: str) -> web.StreamResponse:
        """A picture, clip or calendar a card fetches from Home Assistant's REST API.

        The page steers every such request here. If the path is one the
        published views entitle a visitor to (a published camera's picture, a
        listed Frigate event's thumbnail...), the visitor is sent on to a
        link signed for that one path, valid for a minute, as the read-only
        viewer. Anything else simply does not exist.
        """
        from homeassistant.components.http.auth import async_sign_path

        from . import mirror

        path = "/" + route
        if request.query_string:
            path += "?" + request.query_string
        access = await self._coordinator.async_access()
        if access.stream_socket_allowed(path):
            return await self._serve_stream(request, path)
        if access.stream_info_allowed(path):
            # Fetched here and passed on cleaned, never handed over as a link:
            # it names the camera's address on the home network.
            _, token = await mirror.async_get_viewer(self.hass)
            signed = async_sign_path(
                self.hass, path, timedelta(seconds=30), refresh_token_id=token.id
            )
            info = await ha_data.async_fetch_own_json(self.hass, signed)
            if info is None:
                response = self._json({"message": "Not available"}, status=502)
            else:
                response = self._json(clean_stream_info(info))
            response.headers["Cache-Control"] = "no-store"
            return response
        if access.llmvision_events_allowed(path):
            # Fetched here and passed on filtered: the timeline holds every
            # camera's events, and a visitor gets the published cameras'.
            _, token = await mirror.async_get_viewer(self.hass)
            signed = async_sign_path(
                self.hass, path, timedelta(seconds=30), refresh_token_id=token.id
            )
            timeline = await ha_data.async_fetch_own_json(self.hass, signed)
            if timeline is None:
                response = self._json({"message": "Not available"}, status=502)
            else:
                response = self._json(access.llmvision_events(timeline))
            response.headers["Cache-Control"] = "no-store"
            return response
        if not access.path_allowed(path):
            self._coordinator.note_refusal(
                f"Request not served for a visitor (not published): {path[:200]}"
            )
            response = self._json({"message": "Not found"}, status=404)
            response.headers["Cache-Control"] = "no-store"
            return response
        _, token = await mirror.async_get_viewer(self.hass)
        signed = async_sign_path(
            self.hass, path, timedelta(seconds=60), refresh_token_id=token.id
        )
        response = web.Response(status=302, headers={"Location": signed})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _serve_stream(self, request: web.Request, path: str) -> web.StreamResponse:
        """A published camera's live stream, to watch and to listen to.

        The stream's socket is go2rtc's. Besides sending video it accepts a
        WebRTC offer that carries the browser's microphone, and plays it on
        the camera's loudspeaker; it also takes commands this page has no
        use for. So a visitor never gets a link to it. It is opened here, as
        the read-only viewer, and of what the visitor's side sends only
        requests to watch go through, a WebRTC offer made receive-only
        (``policy.stream_request``). Video comes back untouched.
        """
        from homeassistant.components.http.auth import async_sign_path

        from . import mirror

        visitor = guard.visitor_address(request)
        if request.headers.get("Upgrade", "").lower() != "websocket":
            response = self._json({"message": "Not found"}, status=404)
            response.headers["Cache-Control"] = "no-store"
            return response
        if not self._streams.acquire(visitor):
            response = web.Response(text="503: Too many streams", status=503)
            response.headers["Retry-After"] = "30"
            return response
        upstream = None
        try:
            _, token = await mirror.async_get_viewer(self.hass)
            signed = async_sign_path(
                self.hass, path, timedelta(seconds=30), refresh_token_id=token.id
            )
            upstream = await ha_data.async_open_own_socket(self.hass, signed)
            if upstream is None:
                response = self._json({"message": "Not available"}, status=502)
                response.headers["Cache-Control"] = "no-store"
                return response
            socket = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_STREAM_REQUEST)
            await socket.prepare(request)

            async def from_visitor() -> None:
                async for message in socket:
                    if message.type != WSMsgType.TEXT:
                        # go2rtc is sent text only; anything else is not a
                        # request to watch.
                        break
                    passed = stream_request(message.data)
                    if passed is None:
                        self._coordinator.note_refusal(
                            "Live stream: a message from a visitor was not passed on"
                            f" to go2rtc: {str(message.data)[:120]}"
                        )
                        continue
                    await upstream.send_str(passed)

            async def to_visitor() -> None:
                async for message in upstream:
                    if message.type == WSMsgType.BINARY:
                        await socket.send_bytes(message.data)
                    elif message.type == WSMsgType.TEXT:
                        await socket.send_str(message.data)
                    else:
                        break

            tasks = [
                asyncio.ensure_future(from_visitor()),
                asyncio.ensure_future(to_visitor()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await socket.close()
            return socket
        finally:
            if upstream is not None:
                await upstream.close()
            self._streams.release(visitor)

    async def _serve_page(self, request: web.Request, route: str) -> web.StreamResponse:
        from . import mirror

        options = self._options
        if route == "healthz":
            return self._json({"status": "ok", "path": self.public_path, "mode": "mirror"})
        if route == "api/history/period" or route.startswith("api/history/period/"):
            # Cards that read history over REST instead of the websocket
            # (ApexCharts) are steered here by the page; same shape, same
            # answer, but only the published views' entities come back.
            from . import history

            entity_ids = (await self._coordinator.async_access()).entities
            start = route.removeprefix("api/history/period").strip("/") or None
            parsed = history.parse_query(
                start, dict(request.query), entity_ids or set(),
                max_span=history.max_span(self.hass),
            )
            if isinstance(parsed, str):
                return self._json({"message": parsed}, status=400)
            response = self._json(await history.async_fetch(self.hass, parsed))
            # Never reusable: cards ask for "new points up to the chart's end"
            # (ApexCharts with span: start/end: day), and that URL stays the
            # same until a new point arrives. A cached answer would freeze the
            # chart for the cache lifetime; Home Assistant's own history API
            # isn't cacheable either.
            response.headers["Cache-Control"] = "no-store"
            return response
        if route.startswith("api/"):
            return await self._serve_media(request, route)
        html = await mirror.async_render_page(
            self.hass,
            self.public_path,
            options.get(CONF_LOADING_BACKGROUND),
            self._coordinator.show_header,
        )
        if html is None:
            # Defensive: the frontend glue ships with the integration.
            return self._unavailable(
                "This dashboard is being prepared. Please try again in a few minutes."
            )
        response = web.Response(text=html, content_type="text/html")
        # The frontend loads scripts from the same origin and inlines none of
        # ours except the bootstrap, so the policy stays tight but must allow
        # websockets and the frontend's own assets.
        response.headers["Cache-Control"] = "no-store"
        # A visitor looks and listens. The browser is told not to offer this
        # page a microphone or a camera at all, so a camera card's talk
        # button has nothing to send; the stream endpoint refuses it besides.
        response.headers["Permissions-Policy"] = "microphone=(), camera=(), display-capture=()"
        if options.get(CONF_NOINDEX, True):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

