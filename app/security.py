"""Web hardening: security headers + in-memory POST rate limiting.

Rate limiter is per-process memory: correct for the single-uvicorn deploy
this project targets. Behind multiple workers use a shared store instead —
see DEPLOY.md.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from collections import OrderedDict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_POST_LIMIT = (120, 60)  # 120 POSTs per 60s per client
MAX_TRACKED_CLIENTS = 10_000  # LRU bound on the hit table


def csp_header(nonce: str) -> str:
    # Scripts and <style> elements need the per-response nonce, and style=""
    # attributes are blocked: templates style through classes in app.css.
    # (JS setting element.style is CSSOM, which CSP doesn't restrict.)
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


def apply_security_headers(response, nonce: str):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Content-Security-Policy"] = csp_header(nonce)
    if settings.session_secure_cookie:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers on every response, including the 500 for an unhandled error:
    that is rendered here by `error_response(request)` (and logged), since
    Starlette's own 500 handler sits outside every middleware."""

    def __init__(self, app, error_response=None):
        super().__init__(app)
        self.error_response = error_response or (
            lambda request: PlainTextResponse("Internal Server Error", status_code=500)
        )

    async def dispatch(self, request, call_next):
        # Templates read request.state.csp_nonce for inline <script>/<style>.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error on %s %s", request.method, request.url.path)
            response = self.error_response(request)
        return apply_security_headers(response, nonce)


def forwarded_client(headers, hops: int) -> str | None:
    """The client address from X-Forwarded-For, trusting the last `hops`
    entries (the ones our own proxies appended), or None to leave the
    connection's address alone.

    Proxies append, so a client can put anything in the entries to the left
    of theirs: on Cloud Run, Google's front end appends the address it saw,
    which makes the rightmost entry the real client and the leftmost (what
    uvicorn's `--forwarded-allow-ips "*"` uses) whatever the client sent."""
    if hops <= 0:
        return None
    entries = [e.strip() for name, value in headers if name.lower() == b"x-forwarded-for"
               for e in value.decode("latin-1").split(",")]
    entries = [e for e in entries if e]
    if not entries:
        return None
    host = entries[-hops] if len(entries) >= hops else entries[0]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


class ForwardedForMiddleware:
    """Set the request's client address from X-Forwarded-For per
    settings.proxy_hops. Edits the ASGI scope in place, so uvicorn's access
    log shows the same address the rate limiter keys on."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = forwarded_client(scope.get("headers") or [], settings.proxy_hops)
            if host is not None:
                port = scope["client"][1] if scope.get("client") else 0
                scope["client"] = (host, port)
        await self.app(scope, receive, send)


class HitTable:
    """Sliding-window hit log per client key, bounded as an LRU.

    Keys whose window has emptied are dropped on access, and the least
    recently seen key is evicted past max_keys, so memory stays bounded no
    matter how many distinct clients show up.
    """

    def __init__(self, max_keys: int = MAX_TRACKED_CLIENTS):
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._hits)

    def clear(self) -> None:
        self._hits.clear()

    def hit(self, key: str, limit: int, window: float, now: float) -> float | None:
        """Record a hit; return seconds to wait instead when over the limit."""
        hits = self._hits.pop(key, None) or deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            self._hits[key] = hits
            return hits[0] + window - now
        hits.append(now)
        self._hits[key] = hits  # re-inserted: now most recently used
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        return None


def hit_table(app, name: str = "rate_limit_hits") -> HitTable:
    """The app's hit table `name`. Lives on app.state so it dies with the
    app — nothing accumulates at class level across reloads or test apps."""
    table = getattr(app.state, name, None)
    if table is None:
        table = HitTable()
        setattr(app.state, name, table)
    return table


def client_key(request) -> str:
    """Signed-in users are limited per account, so a classroom behind one
    NAT address doesn't share a budget; anonymous POSTs fall back to the IP
    (resolved from X-Forwarded-For by ForwardedForMiddleware, or by
    uvicorn's --forwarded-allow-ips when TRUSTED_PROXY_HOPS is 0)."""
    user_id = request.scope.get("session", {}).get("user_id")
    if user_id:
        return f"user:{user_id}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window POST limiter keyed by user (or IP). 429 + Retry-After."""

    async def dispatch(self, request, call_next):
        if request.method == "POST":
            limit, window = getattr(
                request.app.state, "rate_limit", DEFAULT_POST_LIMIT
            )
            wait = hit_table(request.app).hit(
                client_key(request), limit, window, time.monotonic()
            )
            if wait is not None:
                return PlainTextResponse(
                    "rate limit exceeded",
                    status_code=429,
                    headers={"Retry-After": str(int(wait) + 1)},
                )
        return await call_next(request)
