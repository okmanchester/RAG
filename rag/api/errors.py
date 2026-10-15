"""HTTP error mapping and an outer boundary that never logs exception bodies."""
import logging
import re

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from rag.management.errors import ManagementError, public_error

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


CONFLICTS = {
    "RESOURCE_DISABLED", "BUILD_OWNERSHIP", "BUILD_NOT_READY", "BUILD_INCOMPLETE", "NO_ACTIVE_BUILD",
    "TASK_RETRY_NOT_ALLOWED", "INDEX_SYNC_NOT_FAILED", "PROCESSING_IDENTITY_CHANGED", "INDEX_MODEL_MISMATCH",
    "KB_CODE_EXISTS", "INDEX_BUSY", "WORKER_BUSY",
}
UNAVAILABLE = {
    "SEARCH_UNAVAILABLE", "SEARCH_ERROR", "INDEX_NOT_FOUND", "INDEX_NOT_READY", "INDEX_CORRUPT",
    "PROCESSING_MODELS_MISSING", "PROCESSING_KEY_REQUIRED", "INVALID_SEARCH_CONFIG",
    "ELASTIC_PASSWORD_REQUIRED", "INVALID_PROCESSING_CONFIG", "PROCESSING_TEMPORARY", "SCHEMA_OUTDATED",
}


def error_response(exc):
    if isinstance(exc, APIError):
        return JSONResponse({"error_code": exc.code, "error": exc.message}, status_code=exc.status)
    value = public_error(exc)
    code = value["error_code"]
    status = 500
    if isinstance(exc, SQLAlchemyError):
        status = 503
    elif isinstance(exc, ManagementError):
        if code == "RECORD_NOT_FOUND":
            status = 404
        elif code == "INVALID_DOCUMENT_FORMAT":
            status = 415
        elif code in CONFLICTS:
            status = 409
        elif code in UNAVAILABLE:
            status = 503
        elif code.startswith(("INVALID_", "UNSAFE_", "PATH_")):
            status = 422
    return JSONResponse(value, status_code=status)


async def validation_error(request, exc: RequestValidationError):
    # Pydantic error dictionaries contain raw input. Never serialize or log them.
    malformed = any(item["type"] == "json_invalid" for item in exc.errors())
    return JSONResponse({"error_code": "INVALID_JSON" if malformed else "INVALID_REQUEST",
                         "error": "invalid JSON input" if malformed else "invalid request parameters"},
                        status_code=400 if malformed else 422)


async def http_error(request, exc: HTTPException):
    code, message = {
        400: ("INVALID_REQUEST", "invalid request body"),
        404: ("NOT_FOUND", "requested route does not exist"),
        405: ("METHOD_NOT_ALLOWED", "method not allowed"),
        413: ("UPLOAD_TOO_LARGE", "upload or request body exceeds the configured limit"),
        415: ("UNSUPPORTED_MEDIA_TYPE", "unsupported request content type"),
    }.get(exc.status_code, ("HTTP_ERROR", "request could not be completed"))
    return JSONResponse({"error_code": code, "error": message}, status_code=exc.status_code,
                        headers=exc.headers)


class SafeBoundary:
    """Bound streamed input before multipart parsing; contain errors before Uvicorn logs them."""
    def __init__(self, app, max_body_bytes, max_json_bytes):
        self.app, self.max_body_bytes, self.max_json_bytes = app, max_body_bytes, max_json_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, received = False, 0
        content_type = next((v.decode("latin-1").split(";", 1)[0].strip().lower()
                             for k, v in scope["headers"] if k.lower() == b"content-type"), "")
        upload = (scope["method"] == "POST" and content_type == "multipart/form-data" and
                  re.fullmatch(r"/api/v1/knowledge-bases/[1-9][0-9]*/documents/?", scope["path"]) is not None)
        limit = self.max_body_bytes if upload else self.max_json_bytes

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413)
            return message

        try:
            lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
            if lengths:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise APIError(400, "INVALID_REQUEST", "invalid request body")
                if int(lengths[0]) > limit:
                    raise APIError(413, "UPLOAD_TOO_LARGE", "upload or request body exceeds the configured limit")
            await self.app(scope, limited_receive, safe_send)
        except Exception as exc:
            # Do not re-raise into ServerErrorMiddleware/Uvicorn's traceback logger.
            if not started:
                response = error_response(exc)
                if response.status_code >= 500:
                    logger.error("HTTP operation failed: %s", exc.code if isinstance(exc, APIError)
                                 else public_error(exc)["error_code"])
                await response(scope, receive, send)
