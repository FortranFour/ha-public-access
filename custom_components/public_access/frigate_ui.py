"""Frigate's own interface for a visitor: placeholder until it is built."""

from __future__ import annotations

from typing import Any

from aiohttp import web


async def async_serve(view: Any, request: web.Request, panel: Any, extra: str) -> web.StreamResponse:
    return web.Response(text="404: Not Found", status=404)
