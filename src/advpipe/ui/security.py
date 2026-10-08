"""Access control for the local web UI: Host check, access token, CSRF, security headers.

The UI can start runs, which spend money and let agents run shell commands, so every request
must prove it comes from the person who started `advpipe ui` (see docs/UI_PLAN.md §5).
"""

from __future__ import annotations

import hashlib
import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-csrf-token"

# Pages load only our own vendored files. htmx is configured (base.html) not to inject inline
# styles or evaluate code, so no 'unsafe-inline' / 'unsafe-eval' is needed.
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}

UNAUTHORIZED = (
    "advpipe ui: access token missing or wrong.\n"
    "Open the link that `advpipe ui` printed in your terminal (it ends in ?token=...).\n"
)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def cookie_name(token: str) -> str:
    """Per-server cookie name. Cookies ignore ports, so two `advpipe ui` servers on different
    ports would otherwise overwrite each other's cookie."""
    return "advpipe_" + hashlib.sha256(token.encode()).hexdigest()[:12]


def host_name(host_header: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '::1'."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _same(given: str | None, expected: str) -> bool:
    return given is not None and secrets.compare_digest(given.encode(), expected.encode())


class GuardMiddleware:
    """Pure ASGI middleware (so it never buffers streamed responses like SSE).

    - Host header must name this machine (``allowed_hosts``; ``None`` allows any), which
      blocks DNS-rebinding pages from talking to the server.
    - ``GET ...?token=T`` with the right token sets an HttpOnly, SameSite=Strict cookie and
      redirects to the same URL without the token. Any other request needs that cookie: 401.
    - Requests other than GET/HEAD/OPTIONS also need the CSRF token in an ``X-CSRF-Token``
      header (base.html makes htmx send it on every request): 403.
    - Every response gets ``SECURITY_HEADERS``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        csrf_token: str,
        allowed_hosts: frozenset[str] | None = LOOPBACK_HOSTS,
    ) -> None:
        self.app = app
        self.token = token
        self.csrf_token = csrf_token
        self.cookie = cookie_name(token)
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":  # the UI has none
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        response = self._check(Request(scope))
        if response is not None:
            await response(scope, receive, send_with_headers)
        else:
            await self.app(scope, receive, send_with_headers)

    def _check(self, request: Request) -> Response | None:
        """A response that refuses (or redirects) the request, or None to let it through."""
        host = host_name(request.headers.get("host", ""))
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            return PlainTextResponse("advpipe ui: unknown Host header.\n", status_code=400)
        given = request.query_params.get("token")
        if given is not None and request.method == "GET":
            if not _same(given, self.token):
                return PlainTextResponse(UNAUTHORIZED, status_code=401)
            return self._login(request)
        if not _same(request.cookies.get(self.cookie), self.token):
            return PlainTextResponse(UNAUTHORIZED, status_code=401)
        if request.method not in SAFE_METHODS and not _same(
            request.headers.get(CSRF_HEADER), self.csrf_token
        ):
            return PlainTextResponse("advpipe ui: missing or wrong CSRF token.\n", status_code=403)
        return None

    def _login(self, request: Request) -> Response:
        """Set the cookie and drop the token from the address bar (and from history)."""
        query = request.url.remove_query_params("token").query
        # Always a local path: "//host" would be a protocol-relative link to another site.
        target = "/" + request.url.path.lstrip("/\\") + (f"?{query}" if query else "")
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(self.cookie, self.token, httponly=True, samesite="strict", path="/")
        return response
