from __future__ import annotations

import base64
import binascii
import hmac
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse

from app.config import Settings


def validate_deployment(settings: Settings) -> None:
    if settings.app_env != "production":
        return
    if not settings.poc_username or ":" in settings.poc_username or not settings.poc_password or len(settings.poc_password.get_secret_value()) < 16:
        raise RuntimeError("Production requires POC_USERNAME and a POC_PASSWORD of at least 16 characters.")
    if not settings.require_persistent_volume or not settings.database_path.is_absolute() or not settings.database_path.parent.is_mount():
        raise RuntimeError("Production requires DATABASE_PATH inside a mounted persistent volume and REQUIRE_PERSISTENT_VOLUME=true.")


class POCSecurityMiddleware:
    """Single-instance POC access control and bounded writes; not a production identity system."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings
        self.requests = defaultdict(deque)
        self.model_starts = deque()
        self.failed_auth = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        path, method = scope.get("path", ""), scope.get("method", "GET")
        production = self.settings.app_env == "production"

        async def reject(status, message, extra_headers=None):
            await PlainTextResponse(message, status_code=status, headers=extra_headers)(scope, receive, send)

        if production and not (method == "GET" and path == "/api/health"):
            now = time.monotonic()
            peer = (scope.get("client") or ("unknown", 0))[0]
            failures = self.failed_auth[peer]
            while failures and failures[0] <= now - 60:
                failures.popleft()
            if len(failures) >= 10:
                return await reject(429, "Authentication rate limit reached; try again shortly.", {"Retry-After": "60"})
            try:
                scheme, encoded = headers.get(b"authorization", b"").split(b" ", 1)
                username, password = base64.b64decode(encoded, validate=True).split(b":", 1)
                valid = (scheme.lower() == b"basic" and hmac.compare_digest(username, self.settings.poc_username.encode())
                         and hmac.compare_digest(password, self.settings.poc_password.get_secret_value().encode()))
            except (ValueError, binascii.Error):
                valid = False
            if not valid:
                failures.append(now)
                return await reject(401, "Authentication required.", {"WWW-Authenticate": 'Basic realm="IncidentPilot", charset="UTF-8"'})

        if production and method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = headers.get(b"origin", b"").decode("latin-1")
            host = headers.get(b"host", b"").decode("latin-1")
            if origin:
                try:
                    parsed_origin = urlsplit(origin)
                    if parsed_origin.scheme != "https" or parsed_origin.netloc != host:
                        return await reject(403, "Cross-origin write denied.")
                except ValueError:
                    return await reject(403, "Invalid origin.")
            if method in {"POST", "PUT", "PATCH"} and not headers.get(b"content-type", b"").lower().startswith(b"application/json"):
                return await reject(415, "JSON content required.")
            now = time.monotonic()
            # The peer address is deliberately not taken from a client-supplied forwarding header.
            peer = (scope.get("client") or ("unknown", 0))[0]
            recent = self.requests[peer]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= 20:
                return await reject(429, "Write rate limit reached; try again shortly.", {"Retry-After": "60"})
            recent.append(now)
            if method == "POST" and (path == "/api/investigations" or path.endswith("/branch")):
                while self.model_starts and self.model_starts[0] <= now - 60:
                    self.model_starts.popleft()
                if len(self.model_starts) >= 4:
                    return await reject(429, "Investigation start rate limit reached; try again shortly.", {"Retry-After": "60"})
                self.model_starts.append(now)

            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 16_384:
                    return await reject(413, "Request body too large.")
                if not message.get("more_body", False):
                    break
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)
