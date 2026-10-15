"""Loopback Host and browser-origin checks, independent of CORS response visibility."""
import re
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse


HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)


def local_origin(value):
    """Return a canonical loopback HTTP origin or None. Never resolve DNS names."""
    if not isinstance(value, str):
        return None
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "?" in value or "#" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not HOST.fullmatch(parsed.netloc) or
                parsed.path or parsed.query or parsed.fragment):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        default = 80 if parsed.scheme == "http" else 443
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None and port != default else "")
    except ValueError:
        return None


def browser_origins(values):
    result = []
    for value in values:
        origin = local_origin(value)
        if origin is None:
            raise ValueError("RAG_API_BROWSER_ORIGINS must contain exact loopback HTTP origins")
        if origin not in result:
            result.append(origin)
    return result


class LocalBrowserGuard:
    def __init__(self, app, allowed_origins):
        self.app, self.allowed_origins = app, frozenset(allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        for name, value in scope["headers"]:
            headers.setdefault(name.lower(), []).append(value)

        def one(name):
            values = headers.get(name, [])
            if len(values) != 1:
                return None
            try:
                return values[0].decode("ascii")
            except UnicodeDecodeError:
                return None

        host = one(b"host")
        origin = local_origin(f"{scope.get('scheme', 'http')}://{host}") if host is not None else None
        error = None
        if origin is None:
            error = (400, "INVALID_HOST", "request Host must be a loopback address")
        elif b"origin" in headers:
            requested = local_origin(one(b"origin"))
            if requested is None or requested != origin and requested not in self.allowed_origins:
                error = (403, "ORIGIN_NOT_ALLOWED", "request browser origin is not allowed")
        elif b"sec-fetch-site" in headers and one(b"sec-fetch-site") not in ("same-origin", "none"):
            error = (403, "ORIGIN_NOT_ALLOWED", "cross-origin browser request requires an allowed Origin")
        if error is not None:
            status, code, message = error
            return await JSONResponse({"error_code": code, "error": message}, status_code=status)(scope, receive, send)
        await self.app(scope, receive, send)


class BrowserCORS(CORSMiddleware):
    def preflight_response(self, request_headers):
        response = super().preflight_response(request_headers)
        if response.status_code != 200:
            return JSONResponse({"error_code": "INVALID_CORS_PREFLIGHT", "error": "CORS preflight was rejected"},
                                status_code=response.status_code,
                                headers={key: value for key, value in response.headers.items()
                                         if key.lower() not in ("content-type", "content-length")})
        return response
